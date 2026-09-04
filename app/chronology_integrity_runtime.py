from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import session_runtime


_ORIGINAL_SELECT = None


def _event_turn(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _event_key(item: Dict[str, Any]) -> str:
    return str(
        item.get("event_id")
        or json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _select_chronology_context(
    chronology: Any,
    *,
    relevant_character_ids: List[str],
    location: Any,
) -> List[Dict[str, Any]]:
    selected = _ORIGINAL_SELECT(
        chronology,
        relevant_character_ids=relevant_character_ids,
        location=location,
    )
    events = chronology if isinstance(chronology, list) else []

    # True anchors/critical milestones are intentionally rare and must not age out.
    # `major` remains subject to the existing bounded selector so routine importance
    # inflation cannot make the packet grow like the full chronology again.
    durable = [
        deepcopy(event)
        for event in events
        if isinstance(event, dict)
        and (
            event.get("anchor") is True
            or str(event.get("importance") or "").casefold() in {"anchor", "critical"}
        )
    ]

    combined: Dict[str, Dict[str, Any]] = {}
    for event in [*durable, *selected]:
        if isinstance(event, dict):
            combined[_event_key(event)] = deepcopy(event)
    return sorted(
        combined.values(),
        key=lambda event: (_event_turn(event), str(event.get("event_id") or "")),
    )


def install() -> None:
    global _ORIGINAL_SELECT
    _ORIGINAL_SELECT = session_runtime._select_chronology_context
    session_runtime._select_chronology_context = _select_chronology_context
