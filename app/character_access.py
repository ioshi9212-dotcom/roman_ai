from typing import Any, Dict

from . import relationship_file_runtime, storage
from .npc_intent import active_intents_for


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

    def resolve_character_id(values, raw):
        text = " ".join(str(raw or "").casefold().replace("ё", "е").split())
        if not text:
            return None
        for item in values:
            cid = storage._card_id(item)
            if cid and " ".join(str(cid).casefold().replace("ё", "е").split()) == text:
                return cid
            for alias in storage._card_names(item):
                if " ".join(str(alias).casefold().replace("ё", "е").split()) == text:
                    return cid
        return None

    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    relationship_store = relationship_file_runtime.load(
        root,
        cards=cards,
        state=state,
        pov_id=str(pov.get("character_id") or ""),
    )
    runtime = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    character_state = runtime.get(character_id, {}) if isinstance(runtime.get(character_id), dict) else {}
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    meta = storage._read_json(root / "meta.json", {})
    active_intents = active_intents_for(
        state,
        [character_id],
        current_turn=int(meta.get("turn_number", 0) or 0),
    ).get(character_id, [])

    return {
        "character_id": character_id,
        "card": card,
        "current_state": character_state,
        "pov_familiarity": character_state.get("pov_familiarity") if isinstance(character_state, dict) else None,
        "personal_memory": storage._memory_bucket(memory, character_id),
        "relationship_to_pov": relationship_file_runtime.character_relation(relationship_store, character_id),
        "npc_relationships_director_only": relationship_file_runtime.outgoing_npc_relations(
            relationship_store,
            character_id,
        ),
        "active_intents": active_intents,
        "instruction": (
            "CARD is objective author context. PERSONAL_MEMORY is the authoritative source for what this character personally knows. "
            "ACTIVE_INTENTS are unresolved character-owned follow-ups, suspicions, promises, investigations, plans and blocked goals. They shape initiative but are not required for it: this registered NPC may initiate contact or appearance from their own goals, character, relationships, work, habits or current state without waiting for POV or a saved intent/thread; use an available physical or remote channel. "
            "POV_FAMILIARITY is persistent identity continuity: known/acquainted means POV already knows this person and a first-time introduction is forbidden; encountered means prior co-presence without guaranteed identity knowledge. "
            "When this registered character enters from offscreen, use this same card/ID and describe a recognizable entrance consistent with the card instead of silently turning an anonymous newcomer into this person later. "
            "RELATIONSHIP_TO_POV is this NPC's persistent directed attitude toward POV and must materially affect characterization: wording, tone, initiative, willingness to approach or avoid, trust, suspicion, jealousy, warmth, hostility, physical distance, risk-taking, help, conflict and attention. "
            "NPC_RELATIONSHIPS_DIRECTOR_ONLY contains only this NPC's qualitative directed links to other NPCs from relationships.json. It is directing context, not automatic factual knowledge. "
            "Do not invent POV->NPC feelings and do not use chronology, source canon, another character's memory or hidden card facts as this character's knowledge. In the current scene the character may learn only what they personally see, hear, receive or are explicitly told while present."
        ),
    }
