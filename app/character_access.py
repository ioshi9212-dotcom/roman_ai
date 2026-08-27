from typing import Any, Dict

from . import storage


def get_character_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    card = next((item for item in cards if storage._card_id(item) == character_id), None)
    if card is None:
        raise KeyError(character_id)

    state = storage._read_json(root / "state.json", {})
    runtime = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    character_state = runtime.get(character_id, {}) if isinstance(runtime.get(character_id), dict) else {}
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))

    return {
        "character_id": character_id,
        "card": card,
        "current_state": character_state,
        "memory": storage._memory_bucket(memory, character_id),
        "relationship_to_pov": storage._relationship_hint(state, character_id),
        "instruction": "Use card + current_state + memory + relationship together before writing this character into the scene.",
    }
