from typing import Any, Dict

from . import npc_relationship_runtime, storage
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

    npc_network = npc_relationship_runtime.build_network(
        cards,
        state,
        resolve_character_id=resolve_character_id,
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
        "relationship_to_pov": storage._relationship_hint(state, character_id),
        "npc_relationships_director_only": npc_relationship_runtime.outgoing_relations_for_character(
            npc_network,
            character_id,
        ),
        "active_intents": active_intents,
        "instruction": (
            "CARD is objective author context. PERSONAL_MEMORY is the authoritative source for what this character personally knows. "
            "ACTIVE_INTENTS are unresolved character-owned follow-ups, suspicions, promises, investigations, plans and blocked goals. When an active intent reaches a causal next step, it drives the NPC's initiative without waiting for POV to mention or summon them; use a logical contact or appearance channel supported by state and character logic. "
            "POV_FAMILIARITY is persistent identity continuity: known/acquainted means POV already knows this person and a first-time introduction is forbidden; encountered means prior co-presence without guaranteed identity knowledge. "
            "When this registered character enters from offscreen, use this same card/ID and describe a recognizable entrance consistent with the card instead of silently turning an anonymous newcomer into this person later. "
            "RELATIONSHIP_TO_POV is this NPC's persistent directed attitude toward POV and must materially affect characterization: wording, tone, initiative, willingness to approach or avoid, trust, suspicion, jealousy, warmth, hostility, physical distance, risk-taking, help, conflict and attention. "
            "NPC_RELATIONSHIPS_DIRECTOR_ONLY contains only relationships owned by this character and is directing context, not personal factual knowledge. Use it for behavior only when consistent with the character's actual knowledge; incoming ties live only in the global director network. "
            "Do not invent POV->NPC feelings and do not use chronology, source canon, another character's memory or hidden card facts as this character's knowledge. In the current scene the character may learn only what they personally see, hear, receive or are explicitly told while present."
        ),
    }
