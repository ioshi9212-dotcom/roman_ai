from __future__ import annotations

import json
import secrets
from copy import deepcopy
from typing import Any, Dict

from . import audit_runtime, storage
from .scene_compaction_runtime import audit_scene_context
from .long_horizon_audit import build_macro_payload, cast_audit, relationship_audit


_ORIGINAL_GET_AUDIT = None
FAST_AUDIT_PACKET_VERSION = 11
AUDIT_PACKET_CHARS = 16000


def _turn_evidence(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    result: Dict[str, Any] = {
        "turn_number": int(turn.get("turn_number", 0) or 0),
        "user_input": str(turn.get("user_input") or "")[:1000],
    }
    for key in (
        "chronology", "knowledge_journal_add", "knowledge_add", "experiences_add", "dialogue_memory_add",
        "presence_updates", "relationship_updates", "npc_intent_updates", "story_thread_updates", "character_upserts",
    ):
        value = extracted.get(key)
        if isinstance(value, list) and value:
            result[key] = deepcopy(value)
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    audited_patch: Dict[str, Any] = {}
    for key in ("current", "pov", "characters"):
        value = state_patch.get(key)
        if isinstance(value, dict) and value:
            audited_patch[key] = deepcopy(value)
    if audited_patch:
        result["state_patch"] = audited_patch
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
    relationship_review = relationship_audit(state, turns, character_ids, start_turn, end_turn)
    cast_review = cast_audit(state, character_ids, start_turn, end_turn)
    all_turns = storage._read_turns(root)
    macro_review = build_macro_payload(
        root,
        source=source,
        state=state,
        chronology=chronology,
        turns=all_turns,
        end_turn=end_turn,
    )

    payload = {
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
        "relationship_audit": relationship_review,
        "cast_activity_audit": cast_review,
        "audit_repair_policy": {
            "mandatory_original_turn": True,
            "chronology_add": "Repair keeps the original turn.",
            "knowledge_journal_add": "Plain v5 journal entries keep the original learned turn/date.",
            "knowledge_add": "Legacy knowledge keeps the learned turn.",
            "experiences_add": "Experience keeps the original turn.",
            "dialogue_memory_add": "Dialogue memory keeps the original turn.",
            "npc_intent_updates": "Repair intent only from audited evidence.",
            "scene_compactions": "REQUIRED: cover every audited turn exactly once by real scenes; one dense factual summary per scene.",
            "memory_compactions": "Optional: merge duplicates only if every distinct fact survives.",
        },
        "audit_contract": {
            "exact_range": [start_turn, end_turn],
            "persistent_storage_is_complete": True,
            "do_not_reaudit_entire_novel": True,
            "check": [
                "missing chronology/journal/memory/intents",
                "state, inventory, scene-item, physical-presence or remote-contact contradictions",
                "relationship dimensions/metadata drift",
                "cast last-appearance/last-contact turn and game-day metadata",
                "scene_compactions cover every audited turn once",
                "lossless memory compaction where useful",
            ],
        },
        "instruction": (
            "Проверь только эти 15 ходов: current scene state, предметы/инвентарь, физическое presence и remote contacts, "
            "знания, отношения и last-seen cast metadata. Исправь только доказанные пропуски, сделай scene compaction и один commitAudit."
        ),
    }
    if macro_review is not None:
        payload["macro_audit_60"] = macro_review
        payload["audit_mode"] = "fast_chat_reconciliation_plus_macro_60"
    return payload


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
        "instruction": "Chunk 0 уже включён. Прочитай остальные chunks и сделай один commitAudit.",
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
