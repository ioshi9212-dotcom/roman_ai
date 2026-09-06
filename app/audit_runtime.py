from __future__ import annotations

import json
import secrets
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .runtime_access import runtime_documents


AUDIT_PACKET_VERSION = 5
AUDIT_PACKET_FILE = "audit_packet.json"


def _audit_range(meta: Dict[str, Any]) -> tuple[int, int]:
    end_turn = int(meta.get("turn_number", 0))
    start_turn = max(int(meta.get("last_audit_turn", 0)) + 1, end_turn - 14)
    return start_turn, end_turn


def _event_turn(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _memory_record_turn(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("learned_turn") or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _source_canon_without_cards(source: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(source if isinstance(source, dict) else {})
    result.pop("characters", None)
    return result


def _audit_character_ids(
    cards: List[Dict[str, Any]], state: Dict[str, Any], memory: Dict[str, Any], chronology: Any,
    audit_turns: List[Dict[str, Any]], start_turn: int, end_turn: int,
) -> List[str]:
    selected: List[str] = []
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        selected.append(str(pov["character_id"]))
    selected.extend(str(value) for value in storage._present_character_ids(state) if value)

    turn_text = json.dumps(audit_turns, ensure_ascii=False).casefold()
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        names = [cid, *storage._card_names(card)]
        if any(str(name).casefold() in turn_text for name in names if len(str(name).strip()) >= 2):
            selected.append(cid)

    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    for cid, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        records = [
            *(bucket.get("knowledge", []) if isinstance(bucket.get("knowledge"), list) else []),
            *(bucket.get("experiences", []) if isinstance(bucket.get("experiences"), list) else []),
            *(bucket.get("dialogue_memory", []) if isinstance(bucket.get("dialogue_memory"), list) else []),
        ]
        if any(start_turn <= _memory_record_turn(item) <= end_turn for item in records if isinstance(item, dict)):
            selected.append(str(cid))

    if isinstance(chronology, list):
        for event in chronology:
            if not isinstance(event, dict) or not start_turn <= _event_turn(event) <= end_turn:
                continue
            participants = event.get("participants_present") or event.get("participants") or event.get("character_ids") or []
            if isinstance(participants, str):
                participants = [participants]
            if isinstance(participants, list):
                for value in participants:
                    if isinstance(value, dict):
                        value = value.get("character_id") or value.get("id") or value.get("name")
                    if value:
                        selected.append(str(value))

    valid = {storage._card_id(card) for card in cards}
    return [cid for cid in dict.fromkeys(selected) if cid in valid]


def _audit_memory(memory: Dict[str, Any], character_ids: List[str]) -> Dict[str, Any]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    return {
        "characters": {
            cid: deepcopy(buckets.get(cid, {"knowledge": [], "experiences": [], "dialogue_memory": []}))
            for cid in character_ids
        }
    }


def _audit_chronology(
    chronology: Any,
    character_ids: List[str],
    start_turn: int,
    end_turn: int,
) -> List[Dict[str, Any]]:
    if not isinstance(chronology, list):
        return []
    relevant = set(character_ids)
    in_range: List[Dict[str, Any]] = []
    durable_anchors: List[Dict[str, Any]] = []
    recent_major: List[Dict[str, Any]] = []
    prior_related: List[Dict[str, Any]] = []

    for raw in chronology:
        if not isinstance(raw, dict):
            continue
        event = deepcopy(raw)
        turn = _event_turn(event)
        if start_turn <= turn <= end_turn:
            in_range.append(event)
            continue
        importance = str(event.get("importance") or "").casefold()
        if importance in {"anchor", "critical"} or event.get("anchor") is True:
            durable_anchors.append(event)
            continue
        if importance == "major":
            recent_major.append(event)
            continue
        if turn >= start_turn:
            continue
        participants = event.get("participants_present") or event.get("participants") or event.get("character_ids") or []
        if isinstance(participants, str):
            participants = [participants]
        ids = set()
        if isinstance(participants, list):
            for value in participants:
                if isinstance(value, dict):
                    value = value.get("character_id") or value.get("id") or value.get("name")
                if value:
                    ids.add(str(value))
        if relevant & ids:
            prior_related.append(event)

    combined = [*durable_anchors, *recent_major[-30:], *prior_related[-30:], *in_range]
    seen = set()
    result: List[Dict[str, Any]] = []
    for event in combined:
        key = str(event.get("event_id") or json.dumps(event, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return sorted(result, key=lambda event: (_event_turn(event), str(event.get("event_id") or "")))


def _build_audit_payload(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")

    start_turn, end_turn = _audit_range(meta)
    source = storage._read_json(root / "source.json", {})
    source_canon = _source_canon_without_cards(source)
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    audit_turns = storage.get_turn_range(session_id, start_turn, end_turn)
    character_ids = _audit_character_ids(cards, state, memory, chronology, audit_turns, start_turn, end_turn)
    card_map = {storage._card_id(card): card for card in cards}

    return {
        "audit_packet_version": AUDIT_PACKET_VERSION,
        "session_id": session_id,
        "audit_range": [start_turn, end_turn],
        "runtime_documents_full": runtime_documents(),
        "source_full": source_canon,
        "source_character_cards_omitted_from_transport": True,
        "state_full": state,
        "audit_character_ids": character_ids,
        "character_cards_audit": [deepcopy(card_map[cid]) for cid in character_ids if cid in card_map],
        "character_registry_index": [
            {"character_id": storage._card_id(card), "name": storage._card_name(card), "role": storage._card_role(card)}
            for card in cards if storage._card_id(card)
        ],
        "memory_audit": _audit_memory(memory, character_ids),
        "chronology_audit": _audit_chronology(chronology, character_ids, start_turn, end_turn),
        "audit_turns_full": audit_turns,
        "storage_contract": {
            "persistent_storage_is_complete": True,
            "audit_payload_is_range_scoped": True,
            "source_character_cards_stay_persistent": True,
            "durable_anchors_do_not_age_out": True,
            "instruction": (
                "Railway stores complete source, live cards, personal memory and chronology. The audit contains exact audited turns, involved dossiers/memory, "
                "all true anchor/critical milestones, bounded major/prior continuity and current state/runtime. Nothing is deleted from storage."
            ),
        },
        "instruction": (
            "15-TURN AUDIT. Read EVERY audit chunk before commitAudit. audit_turns_full contains the exact saved turns in the audit range. "
            "Compare those turns against state_full, memory_audit and chronology_audit. Repair only genuine missing or inconsistent durable records. "
            "Never copy objective chronology/source/card knowledge into personal memory unless an exact audited turn proves perception or disclosure."
        ),
    }


def _packet_path(root) -> Any:
    return root / AUDIT_PACKET_FILE


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    start_turn, end_turn = _audit_range(meta)

    packet_path = _packet_path(root)
    packet = storage._read_json(packet_path, {})
    if (
        isinstance(packet, dict)
        and packet.get("audit_range") == [start_turn, end_turn]
        and packet.get("audit_packet_version") == AUDIT_PACKET_VERSION
        and isinstance(packet.get("chunks"), list)
        and packet.get("chunks")
    ):
        chunks = packet["chunks"]
        return {
            "ok": True,
            "audit_id": packet["audit_id"],
            "audit_range": [start_turn, end_turn],
            "chunk_count": len(chunks),
            "total_chars": sum(len(chunk) for chunk in chunks),
            "already_read_chunks": packet.get("read_chunks", []),
            "instruction": "Read every audit batch in order until next_start_index is null, then commitAudit once.",
        }

    payload = _build_audit_payload(session_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
    packet = {
        "audit_id": secrets.token_urlsafe(12),
        "audit_range": [start_turn, end_turn],
        "audit_packet_version": AUDIT_PACKET_VERSION,
        "chunk_count": len(chunks),
        "read_chunks": [],
        "chunks": chunks,
    }
    storage._write_json(packet_path, packet)
    return {
        "ok": True,
        "audit_id": packet["audit_id"],
        "audit_range": [start_turn, end_turn],
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "already_read_chunks": [],
        "instruction": "Read every audit batch in order until next_start_index is null, then commitAudit once.",
    }


def get_audit_snapshot_chunk(session_id: str, audit_id: str, chunk_index: int) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    packet = storage._read_json(_packet_path(root), {})
    if not packet or packet.get("audit_id") != audit_id:
        raise PermissionError("INVALID_AUDIT_PACKET")
    chunks = packet.get("chunks", [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")

    read_chunks = set(packet.get("read_chunks", []))
    read_chunks.add(chunk_index)
    packet["read_chunks"] = sorted(read_chunks)
    storage._write_json(_packet_path(root), packet)
    return {
        "audit_id": audit_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": len(read_chunks) == len(chunks),
    }


def require_complete_audit_read(session_id: str, start_turn: int, end_turn: int) -> None:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(_packet_path(root), {})
    if not packet or packet.get("audit_range") != [int(start_turn), int(end_turn)]:
        raise RuntimeError("AUDIT_PACKET_REQUIRED")
    chunks = packet.get("chunks", [])
    if len(set(packet.get("read_chunks", []))) < len(chunks):
        raise RuntimeError("AUDIT_PACKET_INCOMPLETE")


def clear_audit_packet(session_id: str) -> None:
    path = _packet_path(storage.SESSIONS_DIR / session_id)
    if path.exists():
        path.unlink()
