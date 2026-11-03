from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from . import session_documents, storage
from .npc_intent import active_intents_for
from .scene_compaction_runtime import active_memory_records, complete_knowledge_records, covered_turns, load_scene_history


WRITER_FIRST_VERSION = 15
WRITER_PACKET_CHARS = 16000
RECENT_FULL_TURNS = 2
CONTINUITY_WINDOW = 15
MAX_WORKING_KNOWLEDGE = 12
MAX_WORKING_EXPERIENCES = 10
MAX_WORKING_DIALOGUE = 10
MAX_HISTORICAL_KNOWLEDGE_CATALOG = 8
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
        "ordered_segments_authoritative": True,
        "spoken_segments": [row["text"] for row in ordered if row["kind"] == "spoken"],
        "stage_directions": [row["text"] for row in ordered if row["kind"] == "stage_direction"],
        "parenthetical_privacy": {
            "private_pov_context": True,
            "not_spoken": True,
            "not_character_knowledge": True,
            "observable_physical_effects_only": True,
        },
        "unclosed_parenthesis": depth > 0,
    }


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


def _compact_thread(item: Any, *, notes_chars: int = 700) -> Any:
    result = deepcopy(item)
    if not isinstance(result, dict):
        return result
    # Keep all structural thread fields, but do not let old free-form notes dominate every turn packet.
    # Full persistent thread state remains in storage.
    notes = result.get("notes")
    if isinstance(notes, str) and len(notes) > notes_chars:
        result["notes"] = notes[:notes_chars]
        result["notes_truncated_in_writer_context"] = True
    return result


def _active_threads(value: Any) -> Any:
    if isinstance(value, dict):
        active = [(str(key), item) for key, item in value.items() if _status(item) not in _TERMINAL]
        # Keep every open line; share a small budget for auxiliary prose notes.
        notes_chars = min(700, 6000 // max(1, len(active)))
        rows = [(key, _compact_thread(item, notes_chars=notes_chars)) for key, item in active]
        rows.sort(key=lambda pair: _priority(pair[1]), reverse=True)
        return dict(rows)
    if isinstance(value, list):
        active = [item for item in value if _status(item) not in _TERMINAL]
        notes_chars = min(700, 6000 // max(1, len(active)))
        rows = [_compact_thread(item, notes_chars=notes_chars) for item in active]
        rows.sort(key=_priority, reverse=True)
        return rows
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
    return active_memory_records(values)[-limit:]


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
        # Knowledge is the factual source for character dialogue, so do not apply
        # a recency cap here. Chunking handles transport size safely.
        bucket["knowledge"] = deepcopy(complete_knowledge_records(bucket.get("knowledge")))
        bucket["experiences"] = _tail(bucket.get("experiences"), MAX_WORKING_EXPERIENCES)
        bucket["dialogue_memory"] = _tail(bucket.get("dialogue_memory"), MAX_WORKING_DIALOGUE)
        bucket["historical_knowledge_catalog"] = []
        omitted = {
            "knowledge": 0,
            "experiences": max(0, counts["experiences"] - len(bucket["experiences"])),
            "dialogue_memory": max(0, counts["dialogue_memory"] - len(bucket["dialogue_memory"])),
        }
        bucket["knowledge_complete_in_transport"] = True
        if omitted["experiences"] or omitted["dialogue_memory"]:
            bucket["older_history_available"] = {
                "knowledge_records_not_full": 0,
                "experience_records_not_full": omitted["experiences"],
                "dialogue_records_not_full": omitted["dialogue_memory"],
                "historical_catalog_truncated": False,
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
    anchors = [event for event in events if _is_anchor(event)]
    anchors.sort(key=lambda event: (_event_turn(event), str(event.get("event_id") or "")))
    result: List[Dict[str, Any]] = []
    for event in anchors:
        text = event.get("event") or event.get("summary") or event.get("fact") or event.get("description") or ""
        row = {
            "event_id": event.get("event_id"),
            "turn_number": _event_turn(event),
            "summary": " ".join(str(text).split())[:MAX_ANCHOR_SUMMARY],
            "participants_present": list(_event_participants(event))[:8],
            "location": event.get("location") or event.get("location_id") or event.get("place"),
            "compacted_scene_id": event.get("compacted_scene_id"),
        }
        result.append({key: value for key, value in row.items() if value not in (None, "", [], 0)})
    return result


def _compact_chronology(value: Any, character_ids: List[str], location: Any) -> List[Dict[str, Any]]:
    events = [
        deepcopy(item) for item in value
        if isinstance(item, dict) and not item.get("compacted_scene_id")
    ] if isinstance(value, list) else []
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


def _compact_turn_extracted(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    result: Dict[str, Any] = {}
    for key in (
        "chronology", "knowledge_add", "experiences_add", "dialogue_memory_add",
        "presence_updates", "relationship_updates", "npc_intent_updates",
        "story_thread_updates", "character_upserts",
    ):
        value = extracted.get(key)
        if isinstance(value, list) and value:
            result[key] = deepcopy(value[:8])
    current = extracted.get("state_patch", {}).get("current") if isinstance(extracted.get("state_patch"), dict) else None
    if isinstance(current, dict) and current:
        result["current_patch"] = deepcopy(current)
    return result


def _compact_full_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    result = {key: deepcopy(turn[key]) for key in ("turn_number", "user_input", "scene_output") if key in turn}
    if "scene_output" in result:
        result["scene_output"] = _strip_relationship_display(result["scene_output"])
    extracted = _compact_turn_extracted(turn)
    if extracted:
        result["extracted"] = extracted
    return result


def _scene_body_excerpt(value: Any, *, limit: int = 1200) -> str:
    """Keep actual scene dialogue/action, never the choices/footer as continuity.

    Both Roman's minimalist header and the legacy emoji header use separators.
    When the message has no recognized header, retain its plain prose instead.
    Privacy redaction is applied afterwards to this excerpt by the packet layer.
    """
    scene = _strip_relationship_display(value)
    start = 0
    choice = scene.find("Что я могу сделать:")
    cutoff = choice if choice >= 0 else len(scene)
    prefix = scene[:cutoff]
    separators = list(re.finditer(r"(?m)^(?:-{20,}|─{8,})[ ]*$", prefix))
    if separators:
        start = separators[-1].end()
    body = " ".join(scene[start:cutoff].split()).strip()
    return body[-limit:] if body else ""


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
    # A compact event/chronology entry rarely preserves the voice, concrete
    # promise or teasing exchange from an ongoing scene. Keep a short exact
    # excerpt in *addition* to structured facts. The privacy layer redacts
    # scene_tail before transport to the writer.
    scene_excerpt = _scene_body_excerpt(turn.get("scene_output"))
    if scene_excerpt:
        result["scene_tail"] = scene_excerpt
    return result


def _rolling_turn_context(root) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    # The editor needs at most the latest 15 uncovered turns, not every raw
    # turn ever written. Full archives remain intact for auditing and rollback.
    compacted = covered_turns(root)
    # Only the most recent 15 actual turns belong to the rolling window.
    # Looking further back for "uncovered" entries scanned the entire archive
    # immediately after each full 15-turn scene compaction.
    tail = storage._read_turns_backwards(root, count=CONTINUITY_WINDOW)
    # Audit compaction is an archival summary, NOT permission to erase the
    # two most recent full scenes from the writer's working context. Keep them
    # even if an audit has just covered the last turn. Older audited turns are
    # still represented by scene_history and remain available in raw archives.
    latest_numbers = {
        int(turn.get("turn_number", 0) or 0)
        for turn in tail[-RECENT_FULL_TURNS:]
    }
    working_turns = [
        turn for turn in tail
        if int(turn.get("turn_number", 0) or 0) in latest_numbers
        or int(turn.get("turn_number", 0) or 0) not in compacted
    ]
    bridge = storage._read_json(root / "handoff_tail.json", [])
    if not isinstance(bridge, list):
        bridge = []
    # A continuation session starts its technical turn counter at 0, but the
    # exact tail of the source session remains writer-only continuity. As new
    # turns accumulate, they naturally push the bridge out of the 15-turn window.
    combined = [*bridge, *working_turns] if bridge else working_turns
    window = combined[-CONTINUITY_WINDOW:]
    return (
        [_compact_full_turn(turn) for turn in window[-RECENT_FULL_TURNS:]],
        [_compact_continuity_turn(turn) for turn in window[:-RECENT_FULL_TURNS]],
    )


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
    result["scene_history"] = load_scene_history(root)
    result["active_threads"] = _active_threads(result.get("active_threads"))

    character_ids = _scene_ids(result)
    # V5 uses complete canonical documents; only legacy needs compaction.
    if not session_documents.is_native(root):
        _compact_memory(result)
        current = result.get("scene_state", {}).get("current", {}) if isinstance(result.get("scene_state"), dict) else {}
        location = (current.get("location") or current.get("place")) if isinstance(current, dict) else None
        chronology_source = result.get("chronology_recent")
        result["chronology_anchor_catalog"] = _anchor_catalog(chronology_source)
        result["chronology_recent"] = _compact_chronology(chronology_source, character_ids, location)
    current_turn = int(meta.get("turn_number", 0) or 0)
    result["npc_active_intents"] = active_intents_for(
        state,
        character_ids,
        current_turn=current_turn,
    )

    return result
