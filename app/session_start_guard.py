from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List

from . import storage


_ORIGINAL_CREATE_SESSION = storage.create_session
_INSTALLED = False

_CURRENT_CONTAINER_KEYS = (
    "scene_state",
    "current_scene",
    "start",
    "start_scene",
    "starting_scene",
    "initial_scene",
    "opening_scene",
)
_POINTER_ALIASES = {
    "date": ("date", "game_date", "calendar_date", "story_date", "start_date"),
    "time": ("time", "game_time", "story_time", "start_time"),
    "location": (
        "location",
        "place",
        "area",
        "location_name",
        "location_id",
        "start_location",
        "starting_location",
    ),
    "scene": (
        "scene",
        "scene_name",
        "situation",
        "start_scene_name",
        "opening",
        "opening_situation",
    ),
}
_PRESENT_KEYS = (
    "present_characters",
    "present_character_ids",
    "characters_present",
    "present",
    "participants_present",
    "scene_characters",
)


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _character_ref(value: Any) -> Any:
    if isinstance(value, dict):
        return (
            value.get("character_id")
            or value.get("id")
            or value.get("name")
            or value.get("full_name")
        )
    if isinstance(value, (str, int, float)):
        return value
    return None


def _resolve_character_ref(cards: Iterable[Dict[str, Any]], value: Any) -> str | None:
    ref = _character_ref(value)
    if ref is None:
        return None
    needle = _norm(ref)
    for card in cards:
        cid = storage._card_id(card)
        if _norm(cid) == needle:
            return cid
        if any(_norm(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _normalise_present(cards: List[Dict[str, Any]], *values: Any) -> List[str]:
    result: List[str] = []
    for value in values:
        for raw in _as_list(value):
            resolved = _resolve_character_ref(cards, raw)
            if resolved and resolved not in result:
                result.append(resolved)
    return result


def _first_value(source: Any, keys: Iterable[str]) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if _nonempty(value):
            return value
    return None


def _display_scalar(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "name",
        "location_name",
        "location_id",
        "id",
        "title",
        "label",
        "value",
    ):
        if _nonempty(value.get(key)):
            return value.get(key)
    return None


def _merge_pointer_from(current: Dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for canonical, aliases in _POINTER_ALIASES.items():
        if _nonempty(current.get(canonical)):
            continue
        value = _first_value(source, aliases)
        value = _display_scalar(value)
        if _nonempty(value):
            current[canonical] = deepcopy(value)


def _candidate_story_containers(template: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for root in (state, template.get("novel"), template.get("world")):
        if not isinstance(root, dict):
            continue
        result.append(root)
        for key in (
            "start",
            "starting_point",
            "starting_scene",
            "initial_scene",
            "opening",
            "opening_scene",
        ):
            nested = root.get(key)
            if isinstance(nested, dict):
                result.append(nested)
    return result


def normalise_session_start(template: Dict[str, Any]) -> Dict[str, Any]:
    """Repair flexible GPT-authored start shapes before a session is persisted.

    This changes only the isolated session snapshot. It does not rewrite a finalized draft or
    reusable library document on disk. The approach mirrors the older generator's tolerant
    intake: preserve supplied story facts, canonicalize transport shape, and add only neutral
    technical fallbacks when a required runtime pointer is absent.
    """

    result = deepcopy(template if isinstance(template, dict) else {})
    cards = storage._normalise_cards(result.get("characters", []))

    raw_state = result.get("starting_state")
    state = deepcopy(raw_state) if isinstance(raw_state, dict) else {}

    # Build current from common nested scene containers first, then let canonical current win.
    current: Dict[str, Any] = {}
    for key in _CURRENT_CONTAINER_KEYS:
        candidate = state.get(key)
        if isinstance(candidate, dict):
            current = storage._deep_merge(current, candidate)
    canonical_current = state.get("current")
    if isinstance(canonical_current, dict):
        current = storage._deep_merge(current, canonical_current)

    # Lift common flat GPT shapes such as {location, time, present_characters}.
    for source in _candidate_story_containers(result, state):
        _merge_pointer_from(current, source)

    present_sources: List[Any] = []
    for source in [current, state, *_candidate_story_containers(result, state)]:
        if not isinstance(source, dict):
            continue
        for key in _PRESENT_KEYS:
            if key in source:
                present_sources.append(source.get(key))

    runtime_characters = state.get("characters")
    if isinstance(runtime_characters, dict):
        present_sources.append(
            [
                character_id
                for character_id, info in runtime_characters.items()
                if isinstance(info, dict) and info.get("present") is True
            ]
        )

    present = _normalise_present(cards, *present_sources)

    pov_raw = state.get("pov")
    if isinstance(pov_raw, dict):
        pov = deepcopy(pov_raw)
        pov_value = (
            pov.get("character_id")
            or pov.get("id")
            or pov.get("name")
            or pov.get("full_name")
        )
    else:
        pov = {}
        pov_value = pov_raw

    for candidate in (
        pov_value,
        state.get("pov_character_id"),
        state.get("pov_character"),
        result.get("novel", {}).get("pov_character_id")
        if isinstance(result.get("novel"), dict)
        else None,
        result.get("novel", {}).get("pov_character")
        if isinstance(result.get("novel"), dict)
        else None,
        result.get("novel", {}).get("pov") if isinstance(result.get("novel"), dict) else None,
    ):
        resolved = _resolve_character_ref(cards, candidate)
        if resolved:
            pov["character_id"] = resolved
            break

    if not pov.get("character_id"):
        inferred = storage._find_pov_id(result, cards)
        if inferred:
            pov["character_id"] = inferred
        elif cards:
            # Same resilience principle as the old generator: a session needs one operational POV.
            pov["character_id"] = storage._card_id(cards[0])

    pov_id = str(pov.get("character_id") or "")
    if pov_id and pov_id not in present:
        present.insert(0, pov_id)

    # A missing roster must never create an unrecoverable turn-zero session.
    if not present and cards:
        present = [pov_id] if pov_id else [storage._card_id(cards[0])]

    current["present_characters"] = list(dict.fromkeys(item for item in present if item))

    # Last-resort technical pointer, matching the tolerant intake used by the old generator.
    # It is used only when the confirmed setup supplied no date, time or location in any known shape.
    if not any(_nonempty(current.get(key)) for key in ("date", "time", "location")):
        current["location"] = "Стартовая локация"
    if not _nonempty(current.get("scene")):
        current["scene"] = "стартовая сцена"

    state["current"] = current
    state["pov"] = pov
    if not isinstance(state.get("characters"), dict):
        state["characters"] = {}
    if not isinstance(state.get("relationships"), dict):
        state["relationships"] = {}
    if not isinstance(state.get("threads"), (dict, list)):
        state["threads"] = {}
    if not isinstance(state.get("world"), dict):
        state["world"] = {}

    result["starting_state"] = state
    return result


def session_start_status(template: Dict[str, Any]) -> Dict[str, Any]:
    prepared = normalise_session_start(template)
    cards = storage._normalise_cards(prepared.get("characters", []))
    state = prepared.get("starting_state") if isinstance(prepared.get("starting_state"), dict) else {}
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = _normalise_present(cards, current.get("present_characters"))
    reasons: List[str] = []
    if not any(_nonempty(current.get(key)) for key in ("date", "time", "location")):
        reasons.append("current_scene_pointer_missing")
    if not present:
        reasons.append("present_characters_empty")
    if pov_id and pov_id not in present:
        reasons.append("pov_missing_from_present")
    if not pov_id:
        reasons.append("pov_missing")
    return {
        "ready": not reasons,
        "reasons": reasons,
        "current": deepcopy(current),
        "pov_character_id": pov_id or None,
        "present_character_ids": present,
    }


def create_session(novel: Dict[str, Any]) -> Dict[str, Any]:
    prepared = normalise_session_start(novel)
    status = session_start_status(prepared)
    if not status["ready"]:
        raise ValueError("SESSION_START_INVALID:" + ",".join(status["reasons"]))
    return _ORIGINAL_CREATE_SESSION(prepared)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    storage.create_session = create_session
    _INSTALLED = True
