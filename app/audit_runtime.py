from __future__ import annotations

import json
import secrets
from copy import deepcopy
from typing import Any, Dict, List

from . import storage


AUDIT_PACKET_VERSION = 6
AUDIT_PACKET_FILE = "audit_packet.json"
MAX_AUDIT_ANCHORS = 24
MAX_AUDIT_MAJOR = 20
MAX_AUDIT_PRIOR_RELATED = 20


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


def _audit_memory(memory: Dict[str, Any], character_ids: List[str], start_turn: int, end_turn: int) -> Dict[str, Any]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    result: Dict[str, Any] = {"characters": {}}
    for cid in character_ids:
        bucket = buckets.get(cid, {}) if isinstance(buckets.get(cid), dict) else {}
        scoped: Dict[str, Any] = {}
        for field in ("knowledge", "experiences", "dialogue_memory"):
            values = bucket.get(field, []) if isinstance(bucket.get(field), list) else []
            scoped[field] = [
                deepcopy(item)
                for item in values
                if isinstance(item, dict) and start_turn <= _memory_record_turn(item) <= end_turn
            ]
        scoped["persistent_counts"] = {
            field: len(bucket.get(field, [])) if isinstance(bucket.get(field), list) else 0
            for field in ("knowledge", "experiences", "dialogue_memory")
        }
        result["characters"][cid] = scoped
    return result


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

    combined = [
        *durable_anchors[-MAX_AUDIT_ANCHORS:],
        *recent_major[-MAX_AUDIT_MAJOR:],
        *prior_related[-MAX_AUDIT_PRIOR_RELATED:],
        *in_range,
    ]
    seen = set()
    result: List[Dict[str, Any]] = []
    for event in combined:
        key = str(event.get("event_id") or json.dumps(event, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return sorted(result, key=lambda event: (_event_turn(event), str(event.get("event_id") or "")))


def _audit_state(state: Dict[str, Any], character_ids: List[str]) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    result.pop("relationships", None)
    result.pop("relationship_documents", None)
    result.pop("relationship_schemas", None)
    result.pop("threads", None)
    runtime = result.get("characters")
    if isinstance(runtime, dict):
        wanted = set(character_ids)
        result["characters"] = {
            str(cid): deepcopy(info)
            for cid, info in runtime.items()
            if str(cid) in wanted
        }
    return result


def _source_reference(source: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(source[key])
        for key in ("novel_id", "title", "version")
        if key in source
    }


def _build_audit_payload(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")

    start_turn, end_turn = _audit_range(meta)
    source = storage._read_json(root / "source.json", {})
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
        "source_reference": _source_reference(source),
        "state_audit": _audit_state(state, character_ids),
        "audit_character_ids": character_ids,
        "character_cards_audit": [deepcopy(card_map[cid]) for cid in character_ids if cid in card_map],
        "character_registry_index": [
            {"character_id": storage._card_id(card), "name": storage._card_name(card), "role": storage._card_role(card)}
            for card in cards if storage._card_id(card)
        ],
        "memory_audit": _audit_memory(memory, character_ids, start_turn, end_turn),
        "chronology_audit": _audit_chronology(chronology, character_ids, start_turn, end_turn),
        "audit_turns_full": audit_turns,
        "audit_contract": {
            "exact_turns_are_authoritative": True,
            "memory_scope": "Only memory records created/learned inside audit_range are transported. Complete lifetime memory remains persistent.",
            "chronology_scope": "Exact in-range events plus bounded older continuity anchors/major/related context.",
            "state_scope": "Current scene/state without full relationship stores or dormant character runtime payloads.",
            "source_scope": "Source canon remains persistent and unchanged; audit verifies durable records created by these exact saved turns, not the entire novel source on every cycle.",
            "character_bundle_rule": "If an exact older personal-memory detail is needed to resolve an inconsistency, load that character bundle on demand before repairing.",
        },
        "storage_contract": {
            "persistent_storage_is_complete": True,
            "audit_payload_is_range_scoped": True,
            "source_character_cards_stay_persistent": True,
            "lifetime_memory_stays_persistent": True,
            "full_relationship_stores_stay_persistent": True,
            "instruction": (
                "Railway retains complete source, live cards, lifetime personal memory, relationships and chronology. This audit packet intentionally transports only the exact 15-turn evidence and bounded continuity needed to audit it. Nothing is deleted from storage."
            ),
        },
        "instruction": (
            "15-TURN AUDIT. Read EVERY audit chunk before commitAudit. audit_turns_full contains the exact saved turns in the audit range. "
            "Compare those turns against state_audit, memory_audit and chronology_audit. Repair only genuine missing or inconsistent durable records proven by those turns. "
            "Never copy objective chronology/card/source knowledge into personal memory unless an exact audited turn proves perception or disclosure."
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
            "instruction": "Read every audit chunk in order, then commitAudit once.",
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
        "instruction": "Read every audit chunk in order, then commitAudit once.",
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
