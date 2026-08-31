from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage
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


def inject_required_turn_context(
    context: Dict[str, Any],
    *,
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: Any,
) -> Dict[str, Any]:
    """Inject the complete persistent writing context before packet chunking.

    Nothing in these durable sources is shortened or summarized here. The final JSON
    is split into MAX_PACKET_CHARS chunks by session_runtime, so large cards, rules,
    chronology and memories remain byte-for-byte available to the model.
    """
    documents = runtime_documents()
    all_cards = full_character_cards(cards)
    present_cards = present_character_cards(cards, state)

    context["runtime_documents"] = documents
    context["scene_builder"] = documents["scene_builder"]
    context["scene_builder_instruction"] = (
        "MANDATORY. Read scene_builder completely before writing and follow its FORMAT exactly. "
        "Do not shorten, reorder, omit or replace its blocks."
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
        "memory_full contains the complete saved personal memory of every character. Card/chronology/source are author truth, not automatic personal knowledge."
    )
    context["full_context_contract"] = {
        "no_truncation": True,
        "instruction": (
            "The packet contains the full runtime documents, full source/questionnaire/canon, full current state, "
            "all full character cards, all saved character memories and the complete chronology. Read every chunk before writing."
        ),
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = all_cards
    author_context["source_full"] = deepcopy(source)
    author_context["chronology_full"] = deepcopy(chronology)
    context["author_context"] = author_context
    context["character_cards"] = all_cards
    return context
