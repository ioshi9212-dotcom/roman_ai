from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .relationship_runtime import build_relationship_lens
from .runtime_access import runtime_documents


def full_character_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"character_id": storage._card_id(card), "card": deepcopy(card)}
        for card in cards
        if storage._card_id(card)
    ]


def present_character_cards(cards: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    present = set(storage._present_character_ids(state))
    return [
        {"character_id": storage._card_id(card), "card": deepcopy(card)}
        for card in cards
        if storage._card_id(card) in present
    ]


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
    return str(
        card.get("name")
        or card.get("full_name")
        or identity.get("name")
        or fallback
    )


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


def _session_persistent_data(context: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any], Any]:
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    session_id = session.get("session_id")
    if not session_id:
        return {}, {"characters": {}}, []
    root = storage.SESSIONS_DIR / str(session_id)
    return (
        storage._read_json(root / "source.json", {}),
        storage._normalise_memory(storage._read_json(root / "memory.json", {})),
        storage._read_json(root / "chronology.json", []),
    )


def inject_required_turn_context(context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Inject complete durable context before the JSON is split into chunks.

    Full rules, source, cards, state, memories and chronology are preserved. Chunking
    happens only after serialization, so nothing here is shortened or summarized.
    """
    source, memory, chronology = _session_persistent_data(context)
    documents = runtime_documents()
    all_cards = full_character_cards(cards)
    present_cards = present_character_cards(cards, state)

    context["runtime_documents"] = documents
    context["scene_builder"] = documents["scene_builder"]
    context["scene_builder_instruction"] = (
        "MANDATORY. Read scene_builder completely before writing and follow its FORMAT exactly. "
        "Do not shorten, reorder, omit or replace its blocks."
    )
    context["pov_participation_contract"] = documents["pov_contract"]
    context["pov_participation_instruction"] = (
        "MANDATORY GLOBAL POV RULE. POV must remain an active participant throughout the scene. "
        "Write ordinary in-character POV dialogue, reactions, thoughts and small actions without asking permission; "
        "do not reduce POV to silence, one-word replies or body-only reactions merely to preserve player agency. "
        "Stop only before genuinely consequential POV choices defined by the contract."
    )
    context["npc_agency_contract"] = documents["npc_agency_contract"]
    context["npc_agency_instruction"] = (
        "MANDATORY GLOBAL NPC AGENCY RULE. NPC behavior comes from that NPC's character, desires, goals, advantage, fears, "
        "relationships, knowledge, duties and current situation, NOT from universal therapy, boundary etiquette or author-approved "
        "psychological correctness. Do not automatically soften, restrain or make NPCs ask permission. If the specific NPC would act, "
        "let them act: intervene, grab a hand/wrist, block a path, take an item, raise their voice, order, pressure, hug or initiate a kiss "
        "without a preliminary permission question when consistent with the character and scene. Do not praise restraint as 'better' or "
        "narrate 'wanted to but did not' merely to model healthy boundaries. Consequential POV reactions and choices remain with the player."
    )
    context["relationship_contract"] = documents["relationship_contract"]
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

    context["source_full"] = deepcopy(source)
    context["state_full"] = deepcopy(state)
    context["memory_full"] = deepcopy(memory)
    context["chronology_full"] = deepcopy(chronology)
    context["all_character_cards"] = all_cards
    context["present_character_cards"] = present_cards
    context["character_card_instruction"] = (
        "all_character_cards contains the complete live card of EVERY registered character. "
        "present_character_cards is the complete-card subset physically present at turn start. "
        "memory_full contains the complete saved personal memory of every character. "
        "Card/chronology/source/hidden lore are AUTHOR TRUTH ONLY and never automatic personal knowledge."
    )
    context["knowledge_guard"] = {
        "mandatory": True,
        "personal_memory_path": "memory_full.characters[character_id]",
        "present_at_turn_start_path": "present_character_ids_at_turn_start",
        "author_only_paths": [
            "source_full",
            "chronology_full",
            "all_character_cards",
            "author_context",
            "runtime_documents",
            "memory_full.characters[OTHER_CHARACTER_ID]",
        ],
        "instruction": (
            "Before EVERY NPC line, inference, recognition or deliberate action, identify that NPC and verify the exact fact source. "
            "Past knowledge must come from that NPC's own memory_full.characters[character_id]. Current-turn knowledge must come "
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
    context["full_context_contract"] = {
        "no_truncation": True,
        "author_truth_is_quarantined_from_character_knowledge": True,
        "instruction": (
            "The packet contains full runtime documents, full source/questionnaire/canon, full current state, "
            "all full character cards, all saved character memories and the complete chronology. Read every chunk before writing. "
            "Full visibility to the AUTHOR does not grant visibility to any character; enforce knowledge_guard per character."
        ),
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = all_cards
    author_context["source_full"] = deepcopy(source)
    author_context["chronology_full"] = deepcopy(chronology)
    author_context["knowledge_quarantine"] = (
        "Everything in author_context is objective author/engine truth only. Never use it as a character knowledge source without "
        "that character's own personal memory or an explicit current-scene perception channel."
    )
    context["author_context"] = author_context
    context["character_cards"] = all_cards
    return context
