from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .character_registry import build_character_registry, normalize_name, refresh_pov_familiarity, registry_instruction
from .turn_context import inject_required_turn_context


RECENT_CHRONOLOGY_EVENTS = 12
CHARACTER_CHRONOLOGY_EVENTS = 5
LOCATION_CHRONOLOGY_EVENTS = 4
ANCHOR_CHRONOLOGY_EVENTS = 24
MAX_CHRONOLOGY_EVENT_CHARS = 700


def _clear_legacy_handoff(root, meta: Dict[str, Any]) -> Dict[str, Any]:
    changed = False
    if meta.get("handoff_required"):
        meta["handoff_required"] = False
        changed = True
    if changed:
        storage._write_json(root / "meta.json", meta)
    for name in ("handoff_tail.json", "resume_token.json"):
        path = root / name
        if path.exists():
            path.unlink()
    return meta


def _resolve_character_id(cards, value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    if not value:
        return None
    needle = normalize_name(value)
    for card in cards:
        cid = storage._card_id(card)
        if normalize_name(cid) == needle:
            return cid
        for alias in storage._card_names(card):
            if normalize_name(alias) == needle:
                return cid
    return None


def _canonicalize_state_character_refs(cards, state: Dict[str, Any]) -> Dict[str, Any]:
    result = storage._deep_merge({}, state if isinstance(state, dict) else {})

    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    resolved = _resolve_character_id(cards, pov.get("character_id")) if isinstance(pov, dict) else None
    if resolved:
        pov["character_id"] = resolved
    result["pov"] = pov

    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    present = current.get("present_characters", [])
    values = list(present.keys()) if isinstance(present, dict) else [present] if isinstance(present, str) else present if isinstance(present, list) else []
    canonical = []
    for value in values:
        resolved = _resolve_character_id(cards, value)
        if resolved:
            canonical.append(resolved)
        elif isinstance(value, dict):
            raw = value.get("character_id") or value.get("id") or value.get("name")
            if raw:
                canonical.append(str(raw))
        elif value:
            canonical.append(str(value))
    current["present_characters"] = list(dict.fromkeys(canonical))
    result["current"] = current
    return result


def _refresh_session_familiarity(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    original_state = storage._read_json(root / "state.json", {})
    state = _canonicalize_state_character_refs(cards, original_state)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    turns = storage._read_turns(root)
    meta = storage._read_json(root / "meta.json", {})
    refreshed = refresh_pov_familiarity(
        cards,
        state,
        memory,
        chronology,
        turns,
        int(meta.get("turn_number", 0)),
    )
    if refreshed != original_state:
        storage._write_json(root / "state.json", refreshed)
    return {
        "root": root,
        "source": source,
        "cards": cards,
        "state": refreshed,
        "memory": memory,
        "chronology": chronology,
        "turns": turns,
        "meta": meta,
    }


def _current_value(state: Dict[str, Any], *keys: str) -> Any:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    for key in keys:
        if current.get(key) not in (None, ""):
            return current.get(key)
    return None


def _period_from_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        hour = int(text.split(":", 1)[0])
    except (TypeError, ValueError):
        return None
    if 5 <= hour <= 11:
        return "утро"
    if 12 <= hour <= 16:
        return "день"
    if 17 <= hour <= 21:
        return "вечер"
    return "ночь"


def _event_turn(event: Dict[str, Any]) -> int:
    try:
        return int(event.get("turn_number") or event.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _event_participants(event: Dict[str, Any]) -> List[str]:
    raw = event.get("participants_present") or event.get("participants") or event.get("character_ids") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    result = []
    for value in raw:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.append(str(value))
    return list(dict.fromkeys(result))


def _event_location(event: Dict[str, Any]) -> str | None:
    value = event.get("location") or event.get("location_id") or event.get("place")
    return str(value) if value not in (None, "") else None


def _event_key(event: Dict[str, Any], index: int) -> str:
    return str(event.get("event_id") or f"turn:{_event_turn(event)}:{index}:{event.get('event', event.get('summary', ''))}")


def _select_chronology_context(
    chronology: Any,
    *,
    relevant_character_ids: List[str],
    location: Any,
) -> List[Dict[str, Any]]:
    if not isinstance(chronology, list):
        return []
    events = [event for event in chronology if isinstance(event, dict)]
    selected: Dict[str, Dict[str, Any]] = {}

    def remember(event: Dict[str, Any], index: int) -> None:
        selected[_event_key(event, index)] = deepcopy(event)

    for index, event in list(enumerate(events))[-RECENT_CHRONOLOGY_EVENTS:]:
        remember(event, index)

    for character_id in relevant_character_ids:
        matches = [
            (index, event)
            for index, event in enumerate(events)
            if str(character_id) in _event_participants(event)
        ][-CHARACTER_CHRONOLOGY_EVENTS:]
        for index, event in matches:
            remember(event, index)

    if location not in (None, ""):
        needle = normalize_name(location)
        matches = [
            (index, event)
            for index, event in enumerate(events)
            if _event_location(event) and normalize_name(_event_location(event)) == needle
        ][-LOCATION_CHRONOLOGY_EVENTS:]
        for index, event in matches:
            remember(event, index)

    anchors = [
        (index, event)
        for index, event in enumerate(events)
        if str(event.get("importance", "")).casefold() in {"anchor", "major", "critical"}
        or event.get("anchor") is True
    ][-ANCHOR_CHRONOLOGY_EVENTS:]
    for index, event in anchors:
        remember(event, index)

    return sorted(selected.values(), key=lambda event: (_event_turn(event), str(event.get("event_id", ""))))


def _normalise_participants(cards, values: Any, fallback: List[str]) -> List[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = fallback
    result = []
    for value in values:
        resolved = _resolve_character_id(cards, value)
        if resolved:
            result.append(resolved)
        elif isinstance(value, dict):
            raw = value.get("character_id") or value.get("id") or value.get("name")
            if raw:
                result.append(str(raw))
        elif value:
            result.append(str(value))
    return list(dict.fromkeys(result))


def _normalise_chronology_events(
    raw_events: Any,
    *,
    turn_number: int,
    state: Dict[str, Any],
    cards,
) -> List[Dict[str, Any]]:
    if not isinstance(raw_events, list):
        raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")

    current_present = storage._present_character_ids(state)
    story_date = _current_value(state, "date", "game_date", "calendar_date")
    story_time = _current_value(state, "time", "game_time")
    location = _current_value(state, "location", "place", "area")
    period = _period_from_time(story_time)
    result: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            continue
        text = raw.get("event") or raw.get("summary") or raw.get("fact") or raw.get("description")
        if text is None:
            continue
        text = " ".join(str(text).split())
        if not text:
            continue
        if len(text) > MAX_CHRONOLOGY_EVENT_CHARS:
            text = text[: MAX_CHRONOLOGY_EVENT_CHARS - 1].rstrip() + "…"

        importance = str(raw.get("importance") or "normal").casefold()
        if importance not in {"normal", "major", "anchor", "critical"}:
            importance = "normal"

        item: Dict[str, Any] = {
            "event_id": str(raw.get("event_id") or f"chrono_t{turn_number}_{index + 1}"),
            "turn_number": turn_number,
            "story_date": raw.get("story_date") or raw.get("date") or story_date,
            "period": raw.get("period") or raw.get("time_of_day") or period,
            "location": raw.get("location") or raw.get("location_id") or raw.get("place") or location,
            "participants_present": _normalise_participants(
                cards,
                raw.get("participants_present") or raw.get("participants"),
                current_present,
            ),
            "event": text,
            "importance": importance,
        }

        if raw.get("time_critical") is True:
            exact_time = raw.get("exact_time") or raw.get("story_time") or raw.get("time") or story_time
            if exact_time not in (None, ""):
                item["exact_time"] = exact_time
            item["time_critical"] = True

        consequences = raw.get("consequences")
        if isinstance(consequences, list):
            compact = [" ".join(str(value).split()) for value in consequences if str(value).strip()]
            if compact:
                item["consequences"] = compact[:4]

        item = {key: value for key, value in item.items() if value not in (None, "", [])}
        result.append(item)

    return result


def _normalise_memory_event_ids(extracted: Dict[str, Any], turn_number: int) -> Dict[str, Any]:
    result = deepcopy(extracted)
    specs = (
        ("knowledge_add", "fact_id", "fact"),
        ("experiences_add", "event_id", "exp"),
        ("dialogue_memory_add", "topic_id", "dialogue"),
    )
    for field, id_key, prefix in specs:
        values = result.get(field)
        if not isinstance(values, list):
            raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")
        normalised = []
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            item.setdefault(id_key, f"{prefix}_t{turn_number}_{index + 1}")
            normalised.append(item)
        result[field] = normalised
    return result


def _prepare_extracted_for_commit(
    payload: Dict[str, Any],
    *,
    root,
    turn_number: int,
) -> Dict[str, Any]:
    extracted = payload.get("extracted")
    if not isinstance(extracted, dict) or extracted.get("persistence_reviewed") is not True:
        raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")

    required_lists = ("chronology", "knowledge_add", "experiences_add", "dialogue_memory_add")
    if any(not isinstance(extracted.get(field), list) for field in required_lists):
        raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    if isinstance(extracted.get("state_patch"), dict):
        state = storage._deep_merge(state, extracted["state_patch"])
    state = _canonicalize_state_character_refs(cards, state)

    result = _normalise_memory_event_ids(extracted, turn_number)
    result["chronology"] = _normalise_chronology_events(
        result.get("chronology"),
        turn_number=turn_number,
        state=state,
        cards=cards,
    )
    return result


def _augment_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return manifest
    context = json.loads(raw)

    snapshot = _refresh_session_familiarity(session_id)
    registry = build_character_registry(snapshot["cards"], snapshot["state"])
    by_id = {row["character_id"]: row for row in registry if row.get("character_id")}

    context["scene_state"] = snapshot["state"]
    context["character_registry"] = registry
    context["character_registry_instruction"] = registry_instruction()

    cast_index = context.get("cast_index", [])
    if isinstance(cast_index, list):
        for row in cast_index:
            if not isinstance(row, dict):
                continue
            registry_row = by_id.get(str(row.get("character_id") or ""))
            if registry_row:
                row["role"] = registry_row.get("role") or row.get("role")
                row["pov_familiarity"] = registry_row.get("pov_familiarity")

    scene_characters = context.get("scene_characters", {})
    if isinstance(scene_characters, dict):
        for cid, bundle in scene_characters.items():
            if not isinstance(bundle, dict):
                continue
            registry_row = by_id.get(str(cid))
            if registry_row:
                bundle["pov_familiarity"] = registry_row.get("pov_familiarity")
                bundle["continuity_rule"] = (
                    "Check pov_familiarity before recognition or introduction. known/acquainted forbids a first-time introduction; "
                    "encountered means prior co-presence but identity may still be unknown."
                )

    context.setdefault("knowledge_boundary", {})["identity_continuity"] = (
        "Who knows a person's identity is separate from objective card truth. Use character_registry.pov_familiarity plus POV personal_memory. "
        "Do not re-introduce known/acquainted characters. Do not name an encountered-but-unknown person through POV until identity is learned."
    )

    relevant_ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
    current_location = _current_value(snapshot["state"], "location", "place", "area")
    chronology_context = _select_chronology_context(
        snapshot["chronology"],
        relevant_character_ids=relevant_ids,
        location=current_location,
    )

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["registered_character_names"] = [
        {"character_id": row.get("character_id"), "name": row.get("name"), "role": row.get("role")}
        for row in registry
    ]
    author_context["chronology_recent"] = chronology_context
    author_context["chronology_context_rule"] = (
        "This is a selected long-range chronology slice, not merely the last turns: recent significant events plus anchors and events relevant to current characters/location. "
        "Use it for objective continuity only. Personal knowledge still comes only from each character's personal_memory and current perception."
    )
    context["author_context"] = author_context
    context = inject_required_turn_context(context, snapshot["cards"], snapshot["state"])

    context["chronology_policy"] = {
        "goal": "Detailed enough for durable canon, compact enough to remain useful after hundreds of turns.",
        "save": (
            "Save only durable objective story facts: introductions and identity reveals, important information exchanged, promises/refusals/deals, conflicts, discoveries, injuries, "
            "relationship-changing actions, arrivals/departures that matter causally, major decisions, plot changes, consequences and facts needed to understand later scenes."
        ),
        "omit": (
            "Do not save ordinary showering, eating, smoking, sitting alone, routine travel, dressing, generic waiting, repeated work/training actions or internal thoughts unless they create a lasting fact, consequence, clue or knowledge change."
        ),
        "granularity": (
            "Usually 0-2 chronology events per turn. Combine several related beats from the same scene into one compact event instead of timestamping every action. "
            "Event text should normally be 1-3 dense sentences and never retell the whole scene."
        ),
        "time": (
            "Store the date and broad period (утро/день/вечер/ночь) when available. Exact clock time is allowed only when causally important: deadline, alibi, appointment, travel timing, attack window, medication timing or another fact whose exact time matters later. "
            "Set time_critical=true only in those cases."
        ),
        "importance": (
            "Use importance=anchor for durable milestones that must remain discoverable far later: first meetings, identity/name acquisition, major revelations, promises/deals, major conflicts, relationship turning points, serious injuries, key decisions and central plot discoveries. "
            "Use major for important but less foundational events; otherwise normal."
        ),
    }
    context["persistence_contract"] = {
        "required": True,
        "instruction": (
            "Before commitTurn review persistence explicitly. extracted MUST contain persistence_reviewed=true and four arrays even when empty: chronology, knowledge_add, experiences_add, dialogue_memory_add. "
            "Do not send extracted={}. If a scene is pure routine and creates no durable fact, chronology may be []. Knowledge/memory arrays may also be empty, but only after checking every present character separately."
        ),
    }

    for key in ("novel", "novel_rules", "novel_lore", "hidden_lore", "story_direction", "world_canon", "character_cards", "relationships", "chronology_recent", "recent_turns"):
        if key in context.get("author_context", {}):
            context[key] = context["author_context"][key]

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(manifest)
    result["chunk_count"] = len(chunks)
    result["character_registry_count"] = len(registry)
    result["full_character_card_count"] = len(snapshot["cards"])
    result["chronology_context_count"] = len(chronology_context)
    result["instruction"] = (
        "Read every chunk before writing. scene_builder is mandatory and its FORMAT must be followed exactly. "
        "The packet contains full cards for every registered character plus personal-memory lenses for scene-relevant characters. "
        "Check character_registry and long-range chronology; before commit review persistence and never send extracted={}."
    )
    return result


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    manifest = storage.prepare_turn_packet(session_id, user_input)
    return _augment_packet(session_id, manifest)


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    turn_number = int(meta.get("turn_number", 0)) + 1
    payload = deepcopy(payload)
    payload["extracted"] = _prepare_extracted_for_commit(payload, root=root, turn_number=turn_number)
    result = storage.commit_turn(session_id, payload)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    result = dict(result)
    result["handoff_required"] = False
    result["saved_chronology_events"] = len(payload["extracted"].get("chronology", []))
    return result


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)

    payload = deepcopy(payload)
    repairs = payload.get("repairs") if isinstance(payload.get("repairs"), dict) else {}
    if isinstance(repairs.get("chronology_add"), list):
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        state = storage._read_json(root / "state.json", {})
        if isinstance(repairs.get("state_patch"), dict):
            state = storage._deep_merge(state, repairs["state_patch"])
        repairs["chronology_add"] = _normalise_chronology_events(
            repairs["chronology_add"],
            turn_number=int(meta.get("turn_number", 0)),
            state=state,
            cards=cards,
        )
        payload["repairs"] = repairs

    result = storage.commit_audit(session_id, payload)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    result = dict(result)
    result["handoff_required"] = False
    return result


def continue_session(session_id: str) -> Dict[str, Any]:
    snapshot = _refresh_session_familiarity(session_id)
    root = snapshot["root"]
    meta = _clear_legacy_handoff(root, snapshot["meta"])
    state = snapshot["state"]
    current = state.get("current", {}) if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    if isinstance(present, str):
        present = [present]
    return {
        "ok": True,
        "session_id": session_id,
        "turn_number": int(meta.get("turn_number", 0)),
        "last_audit_turn": int(meta.get("last_audit_turn", 0) or 0),
        "audit_required": bool(meta.get("audit_required")),
        "current": {
            "date": current.get("date") or current.get("game_date") or current.get("calendar_date"),
            "time": current.get("time") or current.get("game_time"),
            "location": current.get("location") or current.get("place") or current.get("area"),
            "scene": current.get("scene") or current.get("scene_name") or current.get("situation"),
            "present_character_ids": present if isinstance(present, list) else [],
        },
        "character_registry": build_character_registry(snapshot["cards"], state),
        "chronology_context": _select_chronology_context(
            snapshot["chronology"],
            relevant_character_ids=storage._present_character_ids(state),
            location=_current_value(state, "location", "place", "area"),
        ),
        "instruction": (
            "Continue this exact existing session. Nothing was copied, transferred or recreated. "
            "On the next gameplay input call prepareTurn for this same session_id; it will load the exact scene builder, all full character cards, current state, personal memories, live registry, recent turns and compact long-range chronology directly from persistent storage."
        ),
    }
