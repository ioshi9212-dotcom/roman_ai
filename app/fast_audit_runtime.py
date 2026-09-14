from __future__ import annotations

import json
import secrets
from copy import deepcopy
from typing import Any, Dict

from . import audit_runtime, storage
from .scene_compaction_runtime import audit_scene_context


_ORIGINAL_GET_AUDIT = None
FAST_AUDIT_PACKET_VERSION = 9
AUDIT_PACKET_CHARS = 16000


def _turn_evidence(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    result: Dict[str, Any] = {
        "turn_number": int(turn.get("turn_number", 0) or 0),
        "user_input": str(turn.get("user_input") or "")[:1000],
    }
    for key in (
        "chronology", "knowledge_add", "experiences_add", "dialogue_memory_add",
        "presence_updates", "relationship_updates", "npc_intent_updates", "story_thread_updates", "character_upserts",
    ):
        value = extracted.get(key)
        if isinstance(value, list) and value:
            result[key] = deepcopy(value)
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current = state_patch.get("current") if isinstance(state_patch.get("current"), dict) else {}
    if current:
        result["current_patch"] = deepcopy(current)
    scene = str(turn.get("scene_output") or "")
    if scene:
        result["scene_output"] = scene
    return result


def _build_fast_payload(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")

    start_turn, end_turn = audit_runtime._audit_range(meta)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    turns = storage.get_turn_range(session_id, start_turn, end_turn)
    character_ids = audit_runtime._audit_character_ids(
        cards, state, memory, chronology, turns, start_turn, end_turn
    )
    card_map = {storage._card_id(card): card for card in cards}

    return {
        "audit_packet_version": FAST_AUDIT_PACKET_VERSION,
        "audit_mode": "fast_chat_reconciliation",
        "session_id": session_id,
        "audit_range": [start_turn, end_turn],
        "chat_turns_are_primary_review_source": True,
        "turn_evidence_backup": [_turn_evidence(turn) for turn in turns],
        "source_reference": audit_runtime._source_reference(source),
        "state_audit": audit_runtime._audit_state(state, character_ids),
        "audit_character_ids": character_ids,
        "character_cards_audit": [deepcopy(card_map[cid]) for cid in character_ids if cid in card_map],
        "character_registry_index": [
            {"character_id": storage._card_id(card), "name": storage._card_name(card), "role": storage._card_role(card)}
            for card in cards if storage._card_id(card)
        ],
        "memory_audit": audit_runtime._audit_memory(memory, character_ids, start_turn, end_turn),
        "chronology_audit": audit_runtime._audit_chronology(chronology, character_ids, start_turn, end_turn),
        "scene_compaction_context": audit_scene_context(root),
        "audit_repair_policy": {
            "mandatory_original_turn": True,
            "chronology_add": "Each repair must carry turn_number/turn/source_turn from the exact audited turn where the event happened.",
            "knowledge_add": "Each repair must carry learned_turn or turn_number/source_turn from the exact audited turn where the character learned it.",
            "experiences_add": "Each repair must carry turn or turn_number/source_turn from the exact audited turn.",
            "dialogue_memory_add": "Each repair must carry turn or turn_number/source_turn from the exact audited turn.",
            "npc_intent_updates": "Create or repair an intent only when an audited turn proves the NPC formed, advanced, resolved or abandoned that future-facing motive.",
            "scene_compactions": (
                "REQUIRED. Partition the exact audit range into contiguous scenes with no gaps/overlap. "
                "Each row: {scene_id?: existing open scene id only, start_turn, end_turn, summary, participants, location, status}. "
                "summary is ONE dense factual sentence preserving who initiated what, development, important dialogue/revelations/choices, and the ending/pause. "
                "If all 15 turns are one continuous scene, submit exactly one row. If the previous open scene continues, reuse its scene_id and rewrite one updated sentence covering old+new development."
            ),
            "memory_compactions": (
                "OPTIONAL but expected for repeated/verbose memory from this audit range. "
                "Each row: {character_id, memory_type: knowledge|experiences|dialogue_memory, source_ids:[...], summary}. "
                "The summary must preserve every distinct fact contained in the source records. Sources remain raw evidence and are only superseded in working memory."
            ),
        },
        "audit_contract": {
            "exact_range": [start_turn, end_turn],
            "visible_chat_is_primary": True,
            "backup_is_compact": True,
            "persistent_storage_is_complete": True,
            "do_not_reaudit_entire_novel": True,
            "check": [
                "missing important chronology from these 15 turns",
                "missing or unsupported per-character knowledge/memory from these 15 turns",
                "missing or stale NPC follow-up intents created/resolved/advanced in these 15 turns",
                "obvious current-state or presence contradiction with the latest committed scene",
                "scene-level compression of every audited turn without losing distinct facts",
                "duplicate or needlessly fragmented character memory that can be losslessly merged",
            ],
        },
        "instruction": (
            "15-TURN AUDIT + LOSSLESS COMPACTION. Read every persisted scene_output in turn_evidence_backup. "
            "First repair genuine omissions/contradictions. Then group the exact audit range by real scene, not by turn count, and ALWAYS send repairs.scene_compactions covering every audited turn exactly once. "
            "One continuous 15-turn scene becomes ONE dense sentence, not fifteen micro-events and not a vague label. Preserve initiation, development, important dialogue/revelations/choices and the ending/pause. "
            "Use repairs.memory_compactions to merge repeated or fragmented knowledge/experience/dialogue records only when every distinct fact survives in the compact summary. Raw turns and raw source records remain evidence. "
            "Never give a character knowledge merely because chronology/source/card knows it. Commit once after this pass."
        ),
    }


def _response(packet: Dict[str, Any], *, include_first: bool) -> Dict[str, Any]:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    result: Dict[str, Any] = {
        "ok": True,
        "audit_id": packet["audit_id"],
        "audit_range": packet["audit_range"],
        "chunk_count": len(chunks),
        "total_chars": sum(len(str(chunk)) for chunk in chunks),
        "already_read_chunks": packet.get("read_chunks", []),
        "first_chunk_included": bool(include_first and chunks),
        "next_chunk_index": 1 if include_first and len(chunks) > 1 else None,
        "instruction": (
            "Chunk 0 is included in this response when first_chunk_included=true and is already counted as read. "
            "Read only the remaining audit chunks individually, perform one reconciliation + scene compaction pass, then commitAudit once with required repairs.scene_compactions."
        ),
    }
    if include_first and chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(chunks) == 1
    return result


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    start_turn, end_turn = audit_runtime._audit_range(meta)

    path = root / audit_runtime.AUDIT_PACKET_FILE
    packet = storage._read_json(path, {})
    if (
        isinstance(packet, dict)
        and packet.get("audit_range") == [start_turn, end_turn]
        and packet.get("audit_packet_version") == FAST_AUDIT_PACKET_VERSION
        and isinstance(packet.get("chunks"), list)
        and packet.get("chunks")
    ):
        return _response(packet, include_first=False)

    payload = _build_fast_payload(session_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[index:index + AUDIT_PACKET_CHARS] for index in range(0, len(text), AUDIT_PACKET_CHARS)] or ["{}"]
    packet = {
        "audit_id": secrets.token_urlsafe(12),
        "audit_range": [start_turn, end_turn],
        "audit_packet_version": FAST_AUDIT_PACKET_VERSION,
        "chunk_count": len(chunks),
        "read_chunks": [0],
        "chunks": chunks,
        "fast_chat_reconciliation": True,
    }
    storage._write_json(path, packet)
    return _response(packet, include_first=True)


def install() -> None:
    global _ORIGINAL_GET_AUDIT
    if _ORIGINAL_GET_AUDIT is not None:
        return
    _ORIGINAL_GET_AUDIT = audit_runtime.get_audit_snapshot
    audit_runtime.get_audit_snapshot = get_audit_snapshot
