from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage


def _normalise_cards(cards: Any) -> List[Dict[str, Any]]:
    if not isinstance(cards, list):
        return []
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        card = deepcopy(raw)
        cid = storage._card_id(card)
        if not cid or cid in seen:
            continue
        card.setdefault("character_id", cid)
        result.append(card)
        seen.add(cid)
    return result


def _relationship_hint(state: Dict[str, Any], character_id: str) -> Any:
    relationships = state.get("relationships", {}) if isinstance(state, dict) else {}
    if not isinstance(relationships, dict):
        return None
    return relationships.get(character_id) or relationships.get(f"{character_id}->pov")


def install() -> None:
    # Current main still has callers for these helpers. Reconciliation must not
    # silently drop them while the stability storage implementation is active.
    storage._normalise_cards = _normalise_cards
    storage._relationship_hint = _relationship_hint
