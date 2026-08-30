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
        "personal_memory": storage._memory_bucket(memory, character_id),
        "relationship_to_pov": storage._relationship_hint(state, character_id),
        "instruction": (
            "CARD and relationship are author context for characterization. PERSONAL_MEMORY is the authoritative source for what "
            "this character personally knows about past events. Do not use chronology, source canon, another character's memory "
            "or hidden card facts as this character's knowledge. In the current scene the character may learn only what they "
            "personally see, hear, receive or are explicitly told while present."
        ),
    }
