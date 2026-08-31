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
            "The packet contains full runtime documents, full source/questionnaire/canon, full current state, "
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
