from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Dict


_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y")


def _parse_date(value: Any):
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def sync_game_day(state: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    result["current"] = current

    starting = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    starting_current = starting.get("current") if isinstance(starting.get("current"), dict) else {}

    start_date = _parse_date(
        starting_current.get("date")
        or starting_current.get("game_date")
        or starting_current.get("calendar_date")
    )
    current_date = _parse_date(
        current.get("date")
        or current.get("game_date")
        or current.get("calendar_date")
    )

    if start_date is not None and current_date is not None:
        current["game_day"] = max(1, (current_date - start_date).days + 1)
    elif current.get("game_day") in (None, ""):
        current["game_day"] = 1

    return result
