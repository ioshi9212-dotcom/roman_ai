from typing import Any, Dict, List

from . import storage


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, int, float)):
        return [value]
    if isinstance(value, dict):
        # Some generated states store present characters as an id->state map.
        return list(value.keys())
    return []


def _first(value: Any, *keys: str) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) is not None:
            return value.get(key)
    return None


def _character_ref(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        raw = _first(value, "character_id", "id", "name", "full_name")
        return str(raw) if raw is not None else None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _resolve_card(cards: List[Dict[str, Any]], ref: str | None) -> Dict[str, Any] | None:
    if not ref:
        return None
    needle = ref.casefold()
    for card in cards:
        if storage._card_id(card).casefold() == needle:
            return card
        for name in storage._card_names(card):
            if name.casefold() == needle:
                return card
    return None


def get_session_preview(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    meta = _dict(storage._read_json(root / "meta.json", {}))
    source = _dict(storage._read_json(root / "source.json", {}))
    state = _dict(storage._read_json(root / "state.json", {}))
    cards = storage._load_cards(root, source)

    pov_raw = state.get("pov")
    pov_id = _character_ref(pov_raw)
    if not pov_id:
        starting_state = _dict(source.get("starting_state"))
        pov_id = _character_ref(starting_state.get("pov")) or _character_ref(starting_state.get("pov_character_id"))
    if not pov_id:
        novel = _dict(source.get("novel"))
        pov_id = _character_ref(novel.get("pov_character_id")) or _character_ref(novel.get("pov_character")) or _character_ref(novel.get("pov"))
    if not pov_id:
        pov_card_guess = next((card for card in cards if card.get("is_pov") is True or str(card.get("type", "")).casefold() == "pov"), None)
        pov_id = storage._card_id(pov_card_guess) if pov_card_guess else None

    pov_card = _resolve_card(cards, pov_id)
    if pov_card:
        pov_id = storage._card_id(pov_card)

    current = _dict(state.get("current"))
    if not current:
        current = _dict(_dict(source.get("starting_state")).get("current"))

    present = _list(current.get("present_characters"))
    if not present:
        present = _list(current.get("characters"))

    present_names: List[str] = []
    seen = set()
    for value in present:
        ref = _character_ref(value)
        if not ref:
            continue
        card = _resolve_card(cards, ref)
        name = storage._card_name(card) if card else ref
        key = name.casefold()
        if key not in seen:
            present_names.append(name)
            seen.add(key)

    main_cast = []
    for card in cards[:12]:
        main_cast.append(
            {
                "character_id": storage._card_id(card),
                "name": storage._card_name(card),
                "role": storage._card_role(card),
            }
        )

    start_date = _first(current, "date", "game_date", "calendar_date")
    start_time = _first(current, "time", "game_time")
    start_location = _first(current, "location", "place", "area")
    start_scene = _first(current, "scene", "scene_name", "situation")

    return {
        "session_id": session_id,
        "novel_id": meta.get("source_novel_id") or source.get("novel_id"),
        "title": source.get("title"),
        "turn_number": meta.get("turn_number", 0),
        "pov": {
            "character_id": pov_id,
            "name": storage._card_name(pov_card) if pov_card else pov_id,
        },
        "start": {
            "date": start_date,
            "time": start_time,
            "location": start_location,
            "scene": start_scene,
            "present_characters": present_names,
        },
        "character_count": len(cards),
        "main_cast": main_cast,
        "active_threads": state.get("threads") if isinstance(state.get("threads"), (dict, list)) else {},
        "story_direction": source.get("story_direction") if isinstance(source.get("story_direction"), (dict, list, str)) else {},
        "stored_sections": sorted(str(key) for key in source.keys()),
        "instruction": "This preview is built from the actually created session. Missing optional fields are returned as null/empty values instead of failing the preview.",
    }
