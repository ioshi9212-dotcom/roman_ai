from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import storage


AUDIT_RESPONSE_TARGET_CHARS = 24_000
AUDIT_EVENT_LIMIT = 40
AUDIT_MEMORY_LIMIT_PER_KIND = 18
AUDIT_TEXT_LIMIT = 520


def _turn_of(item: Dict[str, Any], primary: str) -> int:
    try:
        return int(item.get(primary) or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _clip(value: Any, limit: int = AUDIT_TEXT_LIMIT) -> Any:
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"
    if isinstance(value, list):
        return [_clip(item, limit) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _clip(item, limit) for key, item in value.items()}
    return deepcopy(value)


def _compact_record(item: Dict[str, Any], *, text_limit: int = AUDIT_TEXT_LIMIT) -> Dict[str, Any]:
    keep = (
        "event_id",
        "fact_id",
        "topic_id",
        "turn_number",
        "turn",
        "learned_turn",
        "date",
        "period",
        "time",
        "location",
        "location_id",
        "participants",
        "participants_present",
        "character_id",
        "confidence",
        "importance",
        "anchor",
        "time_critical",
        "event",
        "summary",
        "fact",
        "description",
        "topic",
        "consequences",
        "source",
        "source_event_ids",
    )
    result: Dict[str, Any] = {}
    for key in keep:
        if key in item and item.get(key) not in (None, "", [], {}):
            result[key] = _clip(item.get(key), text_limit)
    if not result:
        for key, value in item.items():
            if value not in (None, "", [], {}):
                result[str(key)] = _clip(value, text_limit)
    return result


def _cycle_items(items: Any, *, start_turn: int, turn_key: str) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        if _turn_of(raw, turn_key) >= start_turn:
            result.append(raw)
    return result


def _compact_memory(memory: Dict[str, Any], *, start_turn: int, per_kind: int, text_limit: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    recent: Dict[str, Any] = {}
    counts: Dict[str, Any] = {}
    characters = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    for character_id, bucket in characters.items():
        if not isinstance(bucket, dict):
            continue
        raw_knowledge = _cycle_items(bucket.get("knowledge"), start_turn=start_turn, turn_key="learned_turn")
        raw_experiences = _cycle_items(bucket.get("experiences"), start_turn=start_turn, turn_key="turn")
        raw_dialogue = _cycle_items(bucket.get("dialogue_memory"), start_turn=start_turn, turn_key="turn")
        if not (raw_knowledge or raw_experiences or raw_dialogue):
            continue

        knowledge = [_compact_record(item, text_limit=text_limit) for item in raw_knowledge[-per_kind:]]
        experiences = [_compact_record(item, text_limit=text_limit) for item in raw_experiences[-per_kind:]]
        dialogue = [_compact_record(item, text_limit=text_limit) for item in raw_dialogue[-per_kind:]]
        recent[str(character_id)] = {
            "knowledge": knowledge,
            "experiences": experiences,
            "dialogue_memory": dialogue,
        }
        counts[str(character_id)] = {
            "knowledge_total_this_cycle": len(raw_knowledge),
            "experiences_total_this_cycle": len(raw_experiences),
            "dialogue_total_this_cycle": len(raw_dialogue),
            "knowledge_in_snapshot": len(knowledge),
            "experiences_in_snapshot": len(experiences),
            "dialogue_in_snapshot": len(dialogue),
        }
    return recent, counts


def _compact_chronology(chronology: Any, *, start_turn: int, limit: int, text_limit: int) -> tuple[List[Dict[str, Any]], int]:
    if not isinstance(chronology, list):
        return [], 0
    recent = []
    for raw in chronology:
        if not isinstance(raw, dict):
            continue
        turn = _turn_of(raw, "turn_number")
        if turn == 0 or turn >= start_turn:
            recent.append(raw)
    compact = [_compact_record(item, text_limit=text_limit) for item in recent[-limit:]]
    return compact, len(recent)


def _compact_state(state: Dict[str, Any], *, text_limit: int) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    present_ids = storage._present_character_ids(state)
    runtime_characters = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}

    present_state = {
        character_id: _clip(runtime_characters.get(character_id, {}), text_limit)
        for character_id in present_ids
        if isinstance(runtime_characters.get(character_id), dict)
    }
    present_relationships = {
        character_id: _clip(relationships.get(character_id), text_limit)
        for character_id in present_ids
        if isinstance(relationships.get(character_id), dict)
    }
    return {
        "current": _clip(current, text_limit),
        "pov": _clip(pov, text_limit),
        "present_character_ids": present_ids,
        "present_character_state": present_state,
        "present_relationships_to_pov": present_relationships,
    }


def _build_snapshot(root, *, start_turn: int, end_turn: int, event_limit: int, per_kind: int, text_limit: int) -> Dict[str, Any]:
    state = storage._read_json(root / "state.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])

    recent_memory, memory_counts = _compact_memory(
        memory,
        start_turn=start_turn,
        per_kind=per_kind,
        text_limit=text_limit,
    )
    recent_chronology, chronology_total = _compact_chronology(
        chronology,
        start_turn=start_turn,
        limit=event_limit,
        text_limit=text_limit,
    )

    return {
        "audit_range": [start_turn, end_turn],
        "snapshot_mode": "compact_action_safe",
        "state": _compact_state(state, text_limit=text_limit),
        "saved_this_cycle": {
            "chronology": recent_chronology,
            "memory": recent_memory,
        },
        "saved_counts": {
            "chronology_total_this_cycle": chronology_total,
            "chronology_in_snapshot": len(recent_chronology),
            "memory_by_character": memory_counts,
        },
        "instruction": (
            "FAST AUDIT. This snapshot is deliberately compact so ChatGPT Actions cannot fail with ResponseTooLargeError. "
            "Use the last 15 turns already visible in the current chat as the primary scene evidence. Compare them with saved_this_cycle and compact state. "
            "Add only genuinely missing chronology, per-character knowledge/experience/dialogue memory, or an obvious current-state correction. "
            "Never copy chronology into character memory unless that character personally saw, heard, received, read or was told the information. "
            "Do not fetch raw turns for a routine audit. Then call commitAudit once."
        ),
    }


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")

    end_turn = int(meta.get("turn_number", 0))
    start_turn = max(int(meta.get("last_audit_turn", 0)) + 1, end_turn - 14)

    snapshot = _build_snapshot(
        root,
        start_turn=start_turn,
        end_turn=end_turn,
        event_limit=AUDIT_EVENT_LIMIT,
        per_kind=AUDIT_MEMORY_LIMIT_PER_KIND,
        text_limit=AUDIT_TEXT_LIMIT,
    )
    size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    if size > AUDIT_RESPONSE_TARGET_CHARS:
        snapshot = _build_snapshot(
            root,
            start_turn=start_turn,
            end_turn=end_turn,
            event_limit=24,
            per_kind=8,
            text_limit=260,
        )
        size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))

    snapshot["response_chars"] = size
    snapshot["response_target_chars"] = AUDIT_RESPONSE_TARGET_CHARS
    return snapshot
