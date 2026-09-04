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
                    "If saved_dimensions exist, continue exactly this relationship. If empty, evaluate this NPC during the scene. "
                    "Once a real directional attitude toward POV exists, initialize 1-3 natural dimensions from character, goals, knowledge and interaction."
                ),
            }
        )
    return result


def _session_persistent_data(context: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    session_id = session.get("session_id")
    if not session_id:
        return {}, {"characters": {}}
    root = storage.SESSIONS_DIR / str(session_id)
    return (
        storage._read_json(root / "source.json", {}),
        storage._normalise_memory(storage._read_json(root / "memory.json", {})),
    )


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
        "characters": {
            character_id: deepcopy(buckets.get(character_id, {"knowledge": [], "experiences": [], "dialogue_memory": []}))
            for character_id in character_ids
        }
    }


def inject_required_turn_context(context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Inject the complete working context needed for this scene without replaying the whole database."""
    source, memory = _session_persistent_data(context)
    documents = runtime_documents()
    scene_ids = _scene_character_ids(context, state)
    scene_cards = _selected_cards(cards, scene_ids)
    scene_memory = _selected_memory(memory, scene_ids)

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
        "let them act consistently with character and scene. Consequential POV reactions and choices remain with the player."
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
    context["relationship_lens"] = relationship_lens
    context["relationship_lens_instruction"] = (
        "MANDATORY. relationship_lens is authoritative for current NPC->POV relations. Preserve existing dimensions across absences; "
        "initialize 1-3 natural dimensions only when a real attitude is established."
    )

    context["source_full"] = deepcopy(source)
    context["state_full"] = deepcopy(state)
    context["scene_character_ids"] = scene_ids
    context["scene_character_cards"] = scene_cards
    context["scene_character_memory"] = scene_memory
    context["character_registry_index"] = [
        {"character_id": storage._card_id(card), "name": _card_name(card, storage._card_id(card))}
        for card in cards
        if storage._card_id(card)
    ]
    context["character_context_instruction"] = (
        "scene_character_cards and scene_character_memory are COMPLETE for every character relevant at turn start: POV, present cast, "
        "and characters explicitly resolved from the current input. Other registered characters remain fully stored in Railway. "
        "If an offscreen registered character must enter or materially act during this turn, call getCharacterBundle for that character before writing them."
    )
    context["knowledge_guard"] = {
        "mandatory": True,
        "personal_memory_path": "scene_character_memory.characters[character_id]",
        "author_only_paths": [
            "source_full",
            "author_context",
            "runtime_documents",
            "character_registry_index",
            "scene_character_memory.characters[OTHER_CHARACTER_ID]",
        ],
        "instruction": (
            "Before every NPC line, inference, recognition or deliberate action, verify the fact source for that NPC. Past knowledge must "
            "come from that NPC's own personal memory. Current-turn knowledge must come from an explicit perception channel established in "
            "the scene. Private POV thoughts, screens, messages, headphones, letters and photos remain private unless explicitly exposed. "
            "A character arriving later receives no retroactive knowledge. Never repair a generated knowledge leak with narrator justification; rewrite it."
        ),
    }
    context["working_context_contract"] = {
        "persistent_storage_is_complete": True,
        "turn_packet_is_scene_scoped": True,
        "no_persistent_data_deleted": True,
        "instruction": (
            "Railway stores the complete canon, all cards, all personal memories and full chronology. This packet intentionally carries the "
            "complete material needed for the current scene instead of retransmitting unrelated dormant dossiers every turn. Read every packet chunk."
        ),
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = scene_cards
    author_context["knowledge_quarantine"] = (
        "Everything in author_context is objective author/engine truth only. Never use it as character knowledge without that character's own memory or current perception."
    )
    context["author_context"] = author_context
    context["character_cards"] = scene_cards
    return context
