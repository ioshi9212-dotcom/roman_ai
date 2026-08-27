from typing import Any, Dict

from . import storage


def get_session_preview(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    meta = storage._read_json(root / "meta.json", {})
    source = storage._read_json(root / "source.json", {})
    state = storage._read_json(root / "state.json", {})
    cards = storage._load_cards(root, source)

    pov_id = state.get("pov", {}).get("character_id")
    pov_card = next((card for card in cards if storage._card_id(card) == pov_id), None)
    current = state.get("current", {}) if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, str):
        present = [present]

    present_names = []
    for value in present if isinstance(present, list) else []:
        cid = str(value.get("character_id") or value.get("id") or value.get("name")) if isinstance(value, dict) else str(value)
        card = next((item for item in cards if storage._card_id(item) == cid), None)
        present_names.append(storage._card_name(card) if card else cid)

    main_cast = []
    for card in cards[:12]:
        main_cast.append({
            "character_id": storage._card_id(card),
            "name": storage._card_name(card),
            "role": storage._card_role(card),
        })

    return {
        "session_id": session_id,
        "novel_id": meta.get("source_novel_id"),
        "title": source.get("title"),
        "turn_number": meta.get("turn_number", 0),
        "pov": {
            "character_id": pov_id,
            "name": storage._card_name(pov_card) if pov_card else pov_id,
        },
        "start": {
            "date": current.get("date"),
            "time": current.get("time"),
            "location": current.get("location"),
            "scene": current.get("scene"),
            "present_characters": present_names,
        },
        "character_count": len(cards),
        "main_cast": main_cast,
        "active_threads": state.get("threads", {}),
        "story_direction": source.get("story_direction", {}),
        "stored_sections": sorted(source.keys()),
        "instruction": "This preview is built from the actually created session. Show it to the user only after setup verification is complete. Do not claim the session exists without this session_id.",
    }
