from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from . import session_runtime, storage
from .npc_intent import active_intents_for
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
WRITER_FIRST_VERSION = 8
WRITER_PACKET_CHARS = 16000
RECENT_FULL_TURNS = 2
CONTINUITY_WINDOW = 15
MAX_WORKING_KNOWLEDGE = 12
MAX_WORKING_EXPERIENCES = 10
MAX_WORKING_DIALOGUE = 10
MAX_HISTORICAL_KNOWLEDGE_CATALOG = 8
MAX_ACTIVE_THREADS = 12
MAX_RECENT_CHRONOLOGY = 12
MAX_CHARACTER_CHRONOLOGY = 4
MAX_LOCATION_CHRONOLOGY = 4
MAX_FULL_ANCHOR_CHRONOLOGY = 12
MAX_ANCHOR_SUMMARY = 240

_TERMINAL = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}
_RUNTIME_DROP_KEYS = (
    "pov_participation_contract", "npc_agency_contract", "relationship_contract",
    "presence_contract", "memory_contract", "continuity_contract", "writer_contract",
)
_REDUNDANT_INSTRUCTION_KEYS = (
    "knowledge_boundary", "knowledge_guard", "scene_builder_instruction",
    "pov_participation_instruction", "npc_agency_instruction", "character_context_instruction",
    "relationship_lens_instruction", "npc_intent_instruction",
)


def _parse_player_input(text: str) -> Dict[str, Any]:
    ordered: List[Dict[str, str]] = []
    buffer: List[str] = []
    depth = 0

    def flush(kind: str) -> None:
        value = "".join(buffer).strip()
        buffer.clear()
        if value:
            ordered.append({"kind": kind, "text": value})

    for char in str(text or ""):
        if char == "(":
            if depth == 0:
                flush("spoken")
                depth = 1
                continue
            depth += 1
            buffer.append(char)
        elif char == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                flush("stage_direction")
            else:
                buffer.append(char)
        else:
            buffer.append(char)
    flush("stage_direction" if depth else "spoken")
    return {
        "ordered_segments": ordered,
        "spoken_segments": [row["text"] for row in ordered if row["kind"] == "spoken"],
        "stage_directions": [row["text"] for row in ordered if row["kind"] == "stage_direction"],
        "unclosed_parenthesis": depth > 0,
    }


def _compact_cast_index(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    keep = {"character_id", "name", "full_name", "role", "is_pov", "present", "location", "pov_familiarity", "last_seen_turn", "last_interaction_turn"}
    result = []
    for row in value:
        if isinstance(row, dict):
            compact = {key: deepcopy(row[key]) for key in keep if key in row and row[key] not in (None, "", [], {})}
            if compact:
                result.append(compact)
    return result


def _status(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("status", "state", "phase"):
            if value.get(key) not in (None, ""):
                return str(value[key]).casefold().strip()
    return ""


def _priority(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get("priority")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    return {"critical": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}.get(str(raw or "").casefold(), 0)


def _compact_thread(item: Any) -> Any:
    result = deepcopy(item)
    if not isinstance(result, dict):
        return result
    # Keep all structural thread fields, but do not let old free-form notes dominate every turn packet.
    # Full persistent thread state remains in Railway.
    notes = result.get("notes")
    if isinstance(notes, str) and len(notes) > 700:
        result["notes"] = notes[:700]
        result["notes_truncated_in_writer_context"] = True
    return result


def _active_threads(value: Any) -> Any:
    if isinstance(value, dict):
        rows = [(str(key), _compact_thread(item)) for key, item in value.items() if _status(item) not in _TERMINAL]
        rows.sort(key=lambda pair: _priority(pair[1]), reverse=True)
        return {key: item for key, item in rows[:MAX_ACTIVE_THREADS]}
    if isinstance(value, list):
        rows = [_compact_thread(item) for item in value if _status(item) not in _TERMINAL]
        rows.sort(key=_priority, reverse=True)
        return rows[:MAX_ACTIVE_THREADS]
    return deepcopy(value)


def _active_guidance(value: Any) -> Any:
    if isinstance(value, list):
        return [deepcopy(item) for item in value if _status(item) not in _TERMINAL]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, list):
                result[key] = [deepcopy(row) for row in item if _status(row) not in _TERMINAL]
            elif not (isinstance(item, dict) and _status(item) in _TERMINAL):
                result[key] = deepcopy(item)
        return result
    return deepcopy(value)


def _tail(values: Any, limit: int) -> List[Dict[str, Any]]:
    return [deepcopy(item) for item in values if isinstance(item, dict)][-limit:] if isinstance(values, list) else []


def _compact_memory(context: Dict[str, Any]) -> None:
    memory = context.get("character_memory")
    if not isinstance(memory, dict):
        return
    for bucket in memory.values():
        if not isinstance(bucket, dict):
            continue
        counts = {
            "knowledge": len(bucket.get("knowledge", [])) if isinstance(bucket.get("knowledge"), list) else 0,
            "experiences": len(bucket.get("experiences", [])) if isinstance(bucket.get("experiences"), list) else 0,
            "dialogue_memory": len(bucket.get("dialogue_memory", [])) if isinstance(bucket.get("dialogue_memory"), list) else 0,
        }
        bucket["knowledge"] = _tail(bucket.get("knowledge"), MAX_WORKING_KNOWLEDGE)
        bucket["experiences"] = _tail(bucket.get("experiences"), MAX_WORKING_EXPERIENCES)
        bucket["dialogue_memory"] = _tail(bucket.get("dialogue_memory"), MAX_WORKING_DIALOGUE)
        catalog = bucket.get("historical_knowledge_catalog")
        if isinstance(catalog, list):
            bucket["historical_knowledge_catalog"] = deepcopy(catalog[-MAX_HISTORICAL_KNOWLEDGE_CATALOG:])
        omitted = {
            "knowledge": max(0, counts["knowledge"] - len(bucket["knowledge"])),
            "experiences": max(0, counts["experiences"] - len(bucket["experiences"])),
            "dialogue_memory": max(0, counts["dialogue_memory"] - len(bucket["dialogue_memory"])),
        }
        if any(omitted.values()) or (isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG):
            bucket["older_history_available"] = {
                "knowledge_records_not_full": omitted["knowledge"],
                "experience_records_not_full": omitted["experiences"],
                "dialogue_records_not_full": omitted["dialogue_memory"],
                "historical_catalog_truncated": bool(isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG),
                "retrieval": "prepareCharacterBundleRead",
            }


def _event_turn(event: Dict[str, Any]) -> int:
    try:
        return int(event.get("turn_number") or event.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _event_participants(event: Dict[str, Any]) -> set[str]:
    raw = event.get("participants_present") or event.get("participants") or []
    if isinstance(raw, str):
        raw = [raw]
    result = set()
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.add(str(value))
    return result


def _event_location(event: Dict[str, Any]) -> str:
    return str(event.get("location") or event.get("location_id") or event.get("place") or "").casefold().strip()


def _is_anchor(event: Dict[str, Any]) -> bool:
    return str(event.get("importance") or "").casefold() in {"anchor", "critical"} or event.get("anchor") is True


def _anchor_catalog(value: Any) -> List[Dict[str, Any]]:
    events = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    result: List[Dict[str, Any]] = []
    for event in events:
        if not _is_anchor(event):
            continue
        text = event.get("event") or event.get("summary") or event.get("fact") or event.get("description") or ""
        row = {
            "event_id": event.get("event_id"),
            "turn_number": _event_turn(event),
            "summary": " ".join(str(text).split())[:MAX_ANCHOR_SUMMARY],
            "participants_present": list(_event_participants(event))[:8],
            "location": event.get("location") or event.get("location_id") or event.get("place"),
        }
        result.append({key: value for key, value in row.items() if value not in (None, "", [], 0)})
    return result


def _compact_chronology(value: Any, character_ids: List[str], location: Any) -> List[Dict[str, Any]]:
    events = [deepcopy(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    selected: Dict[str, Dict[str, Any]] = {}

    def keep(event: Dict[str, Any], index: int) -> None:
        key = str(event.get("event_id") or f"{_event_turn(event)}:{index}:{event.get('event', '')}")
        selected[key] = event

    for index, event in list(enumerate(events))[-MAX_RECENT_CHRONOLOGY:]:
        keep(event, index)
    for character_id in character_ids:
        for index, event in [(i, e) for i, e in enumerate(events) if character_id in _event_participants(e)][-MAX_CHARACTER_CHRONOLOGY:]:
            keep(event, index)
    needle = str(location or "").casefold().strip()
    if needle:
        for index, event in [(i, e) for i, e in enumerate(events) if _event_location(e) == needle][-MAX_LOCATION_CHRONOLOGY:]:
            keep(event, index)
    full_anchors = [(i, e) for i, e in enumerate(events) if _is_anchor(e)][-MAX_FULL_ANCHOR_CHRONOLOGY:]
    for index, event in full_anchors:
        keep(event, index)
    return sorted(selected.values(), key=lambda event: (_event_turn(event), str(event.get("event_id", ""))))


def _compact_scene_state(value: Any) -> Dict[str, Any]:
    state = deepcopy(value) if isinstance(value, dict) else {}
    state.pop("npc_intents", None)
    return state


def _compact_starting_state(value: Any) -> Dict[str, Any]:
    state = deepcopy(value) if isinstance(value, dict) else {}
    state.pop("npc_intents", None)
    return state


def _strip_relationship_display(scene_output: Any) -> str:
    """Remove historical relationship numbers from writer context, not from stored/player scenes."""
    text = str(scene_output or "")
    marker = text.rfind("\nОтношения:")
    if marker < 0:
        return text
    tail = text[marker:]
    footer = re.search(r"(?m)^\s*Ход\s+\d+\s*·\s*цикл\b.*$", tail)
    if footer is None:
        return text[:marker].rstrip()
    return text[:marker] + "\nОтношения:\n\n" + tail[footer.start():]


def _compact_full_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: deepcopy(turn[key]) for key in ("turn_number", "user_input", "scene_output", "extracted") if key in turn}
    if "scene_output" in result:
        result["scene_output"] = _strip_relationship_display(result["scene_output"])
    return result


def _compact_continuity_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    result: Dict[str, Any] = {"turn_number": int(turn.get("turn_number", 0) or 0), "user_input": str(turn.get("user_input") or "")[:700]}
    if isinstance(extracted.get("chronology"), list) and extracted["chronology"]:
        result["chronology"] = deepcopy(extracted["chronology"][:4])
    for key in ("presence_updates", "npc_intent_updates", "relationship_updates"):
        if isinstance(extracted.get(key), list) and extracted[key]:
            result[key] = deepcopy(extracted[key][:8])
    current = extracted.get("state_patch", {}).get("current") if isinstance(extracted.get("state_patch"), dict) else None
    if isinstance(current, dict) and current:
        result["current_patch"] = deepcopy(current)
    if len(result) == 2:
        scene = " ".join(_strip_relationship_display(turn.get("scene_output")).split())
        if scene:
            result["scene_tail"] = scene[-700:]
    return result


def _rolling_turn_context(root) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    turns = storage._read_turns(root)
    window = turns[-CONTINUITY_WINDOW:]
    return ([_compact_full_turn(turn) for turn in window[-RECENT_FULL_TURNS:]], [_compact_continuity_turn(turn) for turn in window[:-RECENT_FULL_TURNS]])


def _scene_ids(context: Dict[str, Any]) -> List[str]:
    ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
    for row in context.get("character_cards", []) if isinstance(context.get("character_cards"), list) else []:
        if isinstance(row, dict) and row.get("character_id"):
            ids.append(str(row["character_id"]))
    return list(dict.fromkeys(ids))


def _separate_future_guidance(context: Dict[str, Any]) -> None:
    direction = context.pop("story_direction", None)
    author = context.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        if direction in (None, {}, []):
            direction = author.pop("story_direction", None)
        else:
            author.pop("story_direction", None)
        author.pop("instruction", None)
        author.pop("knowledge_quarantine", None)
        context["author_context"] = author
    context["future_guidance"] = {"story_direction": _active_guidance(direction if direction is not None else {}), "status": "future_only_not_history"}


def _strip_instruction_noise(context: Dict[str, Any]) -> None:
    for key in _REDUNDANT_INSTRUCTION_KEYS:
        context.pop(key, None)
    lens = context.get("relationship_lens")
    if isinstance(lens, dict):
        lens = deepcopy(lens)
        lens.pop("initialization_instruction", None)
        for row in lens.get("present_npc_candidates", []) if isinstance(lens.get("present_npc_candidates"), list) else []:
            if isinstance(row, dict):
                row.pop("initialization_rule", None)
        context["relationship_lens"] = lens
    policy = context.get("relationship_policy")
    if isinstance(policy, dict):
        policy = deepcopy(policy)
        policy.pop("authoritative_start_snapshot_note", None)
        context["relationship_policy"] = policy
    contract = context.get("working_context_contract")
    if isinstance(contract, dict):
        contract = deepcopy(contract)
        contract.pop("instruction", None)
        context["working_context_contract"] = contract


def _rewrite_context(session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(context)
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})

    for key in _RUNTIME_DROP_KEYS:
        result.pop(key, None)
    _strip_instruction_noise(result)
    _separate_future_guidance(result)
    result["runtime_document_paths"] = {"rules": "runtime_rules", "scene_builder": "scene_builder"}
    result["player_input_map"] = _parse_player_input(str(result.get("user_input") or ""))

    result["scene_state"] = _compact_scene_state(result.get("scene_state"))
    result["starting_state"] = _compact_starting_state(result.get("starting_state"))
    recent, continuity = _rolling_turn_context(root)
    result["recent_turns"] = recent
    result["continuity_turns"] = continuity
    result["active_threads"] = _active_threads(result.get("active_threads"))
    result["cast_index"] = _compact_cast_index(result.get("cast_index"))
    _compact_memory(result)

    character_ids = _scene_ids(result)
    current = result.get("scene_state", {}).get("current", {}) if isinstance(result.get("scene_state"), dict) else {}
    location = (current.get("location") or current.get("place")) if isinstance(current, dict) else None
    chronology_source = result.get("chronology_recent")
    result["chronology_anchor_catalog"] = _anchor_catalog(chronology_source)
    result["chronology_recent"] = _compact_chronology(chronology_source, character_ids, location)
    result["npc_active_intents"] = active_intents_for(state, character_ids, current_turn=int(meta.get("turn_number", 0) or 0))

    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update({
        "writer_first_version": WRITER_FIRST_VERSION,
        "recent_full_turns": RECENT_FULL_TURNS,
        "continuity_window": CONTINUITY_WINDOW,
        "chronology_selection": {"recent": 12, "per_character": 4, "location": 4, "full_recent_anchors": 12, "all_anchor_catalog": True},
        "working_memory_caps": {"knowledge": MAX_WORKING_KNOWLEDGE, "experiences": MAX_WORKING_EXPERIENCES, "dialogue_memory": MAX_WORKING_DIALOGUE, "historical_knowledge_catalog": MAX_HISTORICAL_KNOWLEDGE_CATALOG},
        "active_thread_cap": MAX_ACTIVE_THREADS,
        "runtime_documents_per_turn": ["runtime_rules", "scene_builder"],
        "future_guidance_is_not_history": True,
        "npc_intents_are_persistent": True,
        "full_npc_intent_store_in_packet": False,
        "first_packet_chunk_in_prepare_response": True,
    })
    result["working_context_contract"] = contract
    return result


def _next_unread(packet: Dict[str, Any]) -> int | None:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    read = {int(value) for value in packet.get("read_chunks", []) if isinstance(value, int)}
    for index in range(1, len(chunks)):
        if index not in read:
            return index
    return None


def _manifest(packet: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    result = dict(base)
    result.update({
        "packet_id": packet.get("packet_id"),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "chunk_count": len(chunks),
        "total_chars": sum(len(str(chunk)) for chunk in chunks),
        "writer_first": True,
        "writer_first_version": WRITER_FIRST_VERSION,
        "chunk_chars_max": WRITER_PACKET_CHARS,
        "first_chunk_included": bool(chunks),
        "next_chunk_index": _next_unread(packet),
        "instruction": "Chunk 0 is included here. Read only remaining unread chunks, then write and commit once.",
    })
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(set(packet.get("read_chunks", []))) >= len(chunks)
    return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    base = dict(_ORIGINAL_PREPARE(session_id, user_input))
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base
        if packet.get("writer_first_version") == WRITER_FIRST_VERSION:
            return _manifest(packet, base)
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        context = _rewrite_context(session_id, json.loads(raw))
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[index:index + WRITER_PACKET_CHARS] for index in range(0, len(text), WRITER_PACKET_CHARS)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["writer_first_version"] = WRITER_FIRST_VERSION
        packet["writer_first_payload_chars"] = len(text)
        storage._write_json(root / "turn_packet.json", packet)
        return _manifest(packet, base)


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
