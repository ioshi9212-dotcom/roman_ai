from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .relationship_runtime import build_relationship_lens
from .runtime_access import runtime_documents


def _normalise_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _resolve_character_id(cards: List[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    needle = _normalise_name(value)
    if not needle:
        return None
    for card in cards:
        cid = storage._card_id(card)
        if _normalise_name(cid) == needle:
            return cid
        if any(_normalise_name(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _card_name(card: Dict[str, Any], fallback: str) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("name") or card.get("full_name") or identity.get("name") or fallback)


def _present_npc_candidates(cards: List[Dict[str, Any]], state: Dict[str, Any], lens: Dict[str, Any]) -> List[Dict[str, Any]]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    by_id = {storage._card_id(card): card for card in cards if storage._card_id(card)}
    saved = {
        str(item.get("owner_character_id")): item
        for item in lens.get("relations_in_current_scene", [])
        if isinstance(item, dict) and item.get("owner_character_id")
    }
    result: List[Dict[str, Any]] = []
    for character_id in storage._present_character_ids(state):
        character_id = str(character_id)
        if not character_id or character_id == pov_id:
            continue
        card = by_id.get(character_id, {})
        relation = saved.get(character_id)
        dimensions = relation.get("dimensions", []) if isinstance(relation, dict) else []
        result.append(
            {
                "character_id": character_id,
                "name": _card_name(card, character_id),
                "has_saved_relationship": bool(dimensions),
                "saved_dimensions": deepcopy(dimensions),
                "initialization_rule": (
                    "If saved_dimensions exist, continue exactly this relationship. If they are empty, this NPC still must be evaluated during the scene. "
                    "As soon as the NPC meaningfully perceives/interacts with POV and a real attitude exists, initialize 1-3 natural relationship dimensions "
                    "from character + goals + knowledge + current interaction and show them in the footer. Do not leave the relationship block empty merely because this is a new chat/session or there was no previous numeric baseline."
                ),
            }
        )
    return result


def _session_memory(context: Dict[str, Any]) -> Dict[str, Any]:
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    session_id = session.get("session_id")
    if not session_id:
        return {"characters": {}}
    root = storage.SESSIONS_DIR / str(session_id)
    return storage._normalise_memory(storage._read_json(root / "memory.json", {}))


def _scene_character_ids(context: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        values.append(str(pov["character_id"]))
    values.extend(str(value) for value in storage._present_character_ids(state) if value)
    values.extend(str(value) for value in context.get("relevant_character_ids", []) if value)
    return list(dict.fromkeys(values))


def _selected_cards(cards: List[Dict[str, Any]], character_ids: List[str]) -> List[Dict[str, Any]]:
    wanted = set(character_ids)
    return [
        {"character_id": storage._card_id(card), "card": deepcopy(card)}
        for card in cards
        if storage._card_id(card) in wanted
    ]


def _selected_memory(memory: Dict[str, Any], character_ids: List[str]) -> Dict[str, Any]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    return {
        character_id: deepcopy(
            buckets.get(character_id, {"knowledge": [], "experiences": [], "dialogue_memory": []})
        )
        for character_id in character_ids
    }


def _deduplicate_scene_character_memory(context: Dict[str, Any]) -> None:
    lenses = context.get("scene_characters")
    if not isinstance(lenses, dict):
        return
    for character_id, lens in lenses.items():
        if not isinstance(lens, dict):
            continue
        lens.pop("personal_memory", None)
        lens["personal_memory_path"] = f"character_memory[{character_id}]"


def inject_required_turn_context(context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Inject each required runtime/canon/memory block once, using stable scene_builder paths."""
    memory = _session_memory(context)
    documents = runtime_documents()
    scene_ids = _scene_character_ids(context, state)
    scene_cards = _selected_cards(cards, scene_ids)
    scene_memory = _selected_memory(memory, scene_ids)

    # Runtime documents are flattened so scene_builder/contracts are not serialized twice.
    context.pop("runtime_documents", None)
    context["runtime_rules"] = documents["rules"]
    context["scene_builder"] = documents["scene_builder"]
    context["pov_participation_contract"] = documents["pov_contract"]
    context["npc_agency_contract"] = documents["npc_agency_contract"]
    context["relationship_contract"] = documents["relationship_contract"]
    context["presence_contract"] = documents["presence_contract"]
    context["memory_contract"] = documents["memory_contract"]
    context["continuity_contract"] = documents["continuity_contract"]
    context["runtime_document_paths"] = {
        "rules": "runtime_rules",
        "scene_builder": "scene_builder",
        "pov_contract": "pov_participation_contract",
        "npc_agency_contract": "npc_agency_contract",
        "relationship_contract": "relationship_contract",
        "presence_contract": "presence_contract",
        "memory_contract": "memory_contract",
        "continuity_contract": "continuity_contract",
    }
    context["scene_builder_instruction"] = (
        "MANDATORY. Read scene_builder completely before writing and follow its FORMAT exactly. "
        "Do not shorten, reorder, omit or replace its blocks."
    )
    context["pov_participation_instruction"] = (
        "MANDATORY GLOBAL POV RULE. POV must remain an active participant throughout the scene. "
        "Write ordinary in-character POV dialogue, reactions, thoughts and small actions without asking permission; "
        "do not reduce POV to silence, one-word replies or body-only reactions merely to preserve player agency. "
        "Stop only before genuinely consequential POV choices defined by the contract."
    )
    context["npc_agency_instruction"] = (
        "MANDATORY GLOBAL NPC AGENCY RULE. NPC behavior comes from that NPC's character, desires, goals, advantage, fears, "
        "relationships, knowledge, duties and current situation, NOT from universal therapy, boundary etiquette or author-approved "
        "psychological correctness. Do not automatically soften, restrain or make NPCs ask permission. If the specific NPC would act, "
        "let them act: intervene, grab a hand/wrist, block a path, take an item, raise their voice, order, pressure, hug or initiate a kiss "
        "without a preliminary permission question when consistent with the character and scene. Do not praise restraint as 'better' or "
        "narrate 'wanted to but did not' merely to model healthy boundaries. Consequential POV reactions and choices remain with the player."
    )

    relationship_lens = build_relationship_lens(
        state,
        cards=cards,
        present_character_ids=storage._present_character_ids,
        resolve_character_id=_resolve_character_id,
    )
    relationship_lens["present_npc_candidates"] = _present_npc_candidates(cards, state, relationship_lens)
    relationship_lens["initialization_required"] = True
    relationship_lens["initialization_instruction"] = (
        "A missing saved relation is NOT a reason to omit relationships forever. Evaluate every present NPC candidate. "
        "For an NPC with saved dimensions, preserve them. For an NPC without saved dimensions, once this scene establishes a real directional attitude toward POV, "
        "create 1-3 specific dimensions natural to that NPC (for example sympathy, suspicion, attraction, respect, irritation, jealousy, trust, resentment, closeness) "
        "and print them in the visible footer. Never use a generic 'interest' placeholder. The first appearance may omit /delta because there is no prior numeric baseline."
    )
    context["relationship_lens"] = relationship_lens
    context["relationship_lens_instruction"] = (
        "MANDATORY. relationship_lens is the old-generator causal relationship layer and is authoritative for current NPC->POV relations. "
        "Every present NPC is listed in present_npc_candidates even when no relationship has been saved yet. "
        "Existing dimensions MUST appear in the visible Relationships footer. Missing dimensions must be initialized when the current interaction actually establishes an attitude; "
        "do not output an empty relationship block simply because this is a new chat or fresh relationship. Carry saved dimensions across absences and later meetings."
    )

    context["character_cards"] = scene_cards
    context["character_memory"] = scene_memory
    _deduplicate_scene_character_memory(context)
    context["character_context_instruction"] = (
        "character_cards and character_memory are COMPLETE for POV, present cast and characters resolved from current input. "
        "These are the stable paths used by scene_builder. character_registry/cast_index remain the compact registry for every registered character. "
        "Other character dossiers and all older persistent data remain fully stored in Railway. If an offscreen registered character must enter or materially act, "
        "call getCharacterBundle before writing that character."
    )
    context["knowledge_guard"] = {
        "mandatory": True,
        "personal_memory_path": "character_memory[character_id]",
        "present_at_turn_start_path": "present_character_ids_at_turn_start",
        "author_only_paths": [
            "character_cards", "novel", "novel_rules", "novel_lore", "hidden_lore", "world_canon",
            "story_direction", "chronology_recent", "character_memory[OTHER_CHARACTER_ID]",
        ],
        "instruction": (
            "Before EVERY NPC line, inference, recognition or deliberate action, identify that NPC and verify the exact fact source. "
            "Past knowledge must come from that NPC's own character_memory[character_id]. Current-turn knowledge must come "
            "from an explicit perception channel established in the scene after turn start. POV thoughts, phone notifications, message "
            "text, screens, headphones, letters/photos held privately and other private POV content stay private unless POV explicitly "
            "shows/reads aloud/forwards/hands them over or the scene already establishes direct visual/auditory access. Mere proximity, "
            "a glance at the phone, an outstretched hand or asking 'show me' does NOT reveal content. A character elsewhere, arriving later "
            "or leaving earlier gets no retroactive knowledge. Inference may use only premises that NPC already knows and may not reproduce "
            "an unknown exact detail. If a drafted NPC line leaks an unsupported fact, rewrite/delete the line. NEVER keep the leak and add "
            "narrator justification such as 'he could infer it' or 'he understood from her reaction'. An invalid generated leak is not canon, "
            "must not be persisted to that NPC's memory, and must not survive into the next turn."
        ),
    }
    context["working_context_contract"] = {
        "persistent_storage_is_complete": True,
        "turn_packet_is_scene_scoped": True,
        "no_persistent_data_deleted": True,
        "single_scene_memory_copy": True,
        "single_runtime_document_copy": True,
        "stable_scene_builder_paths": True,
        "instruction": (
            "Railway stores complete source, live cards, personal memories and chronology. This packet carries one complete working copy "
            "of each required runtime/canon/memory block without retransmitting dormant dossiers or duplicate runtime documents. Read every chunk."
        ),
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = scene_cards
    author_context["knowledge_quarantine"] = (
        "Everything in author_context is objective author/engine truth only. Never use it as a character knowledge source without "
        "that character's own personal memory or an explicit current-scene perception channel."
    )
    context["author_context"] = author_context
    return context
