from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from . import storage


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"


def _read_runtime(name: str) -> str:
    path = RUNTIME_DIR / name
    if not path.exists():
        raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
    return path.read_text(encoding="utf-8")


def full_character_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"character_id": storage._card_id(card), "card": card}
        for card in cards
        if storage._card_id(card)
    ]


def present_character_cards(cards: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    present = set(storage._present_character_ids(state))
    return [
        {"character_id": storage._card_id(card), "card": card}
        for card in cards
        if storage._card_id(card) in present
    ]


def inject_required_turn_context(context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Put non-negotiable writing context directly into the chunked turn packet.

    Custom GPT instructions stay compact. The model must read every packet chunk,
    therefore the exact scene builder and the full live character card set are
    available on every gameplay turn without a separate oversized Action.
    """
    builder = _read_runtime("scene_builder.md")
    all_cards = full_character_cards(cards)
    present_cards = present_character_cards(cards, state)

    context["scene_builder"] = builder
    context["scene_builder_instruction"] = (
        "MANDATORY. Read scene_builder completely before writing. Follow its FORMAT exactly, including header order, separators, "
        "suggestion blocks, State, Relationships and turn/cycle footer. Do not improvise another layout."
    )
    context["all_character_cards"] = all_cards
    context["present_character_cards"] = present_cards
    context["character_card_instruction"] = (
        "all_character_cards contains the complete live card of EVERY registered character in this session. "
        "present_character_cards is the complete-card subset physically present at turn start. Use full cards for characterization, "
        "but never treat card facts as personal knowledge unless that character's personal_memory/current perception supports them."
    )

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = all_cards
    context["author_context"] = author_context
    context["character_cards"] = all_cards
    return context
