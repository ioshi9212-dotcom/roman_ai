from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable


_METRIC_RE = re.compile(r"^(.+?)\s+(-?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)$")


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() else value


def relationship_patch_from_scene(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    state: Dict[str, Any],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    """Persist the relationship values actually shown in the scene footer.

    The visible footer is the authoritative end-of-turn snapshot for present NPC -> POV
    relationship indicators. Deltas are display/audit information; the numeric value before
    `/delta` is the final value that must survive into the next turn.
    """
    if not isinstance(scene_output, str) or "Отношения:" not in scene_output:
        return {}

    lines = scene_output.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Отношения:") + 1
    except StopIteration:
        return {}

    present = set(present_character_ids(state))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    relationships: Dict[str, Dict[str, int | float]] = {}

    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Ход "):
            break
        if " - " not in line:
            continue

        name, raw_metrics = line.split(" - ", 1)
        character_id = resolve_character_id(cards, name.strip())
        if not character_id or character_id == pov_id or character_id not in present:
            continue

        metrics: Dict[str, int | float] = {}
        for raw_metric in raw_metrics.split(";"):
            match = _METRIC_RE.match(raw_metric.strip())
            if not match:
                continue
            metric_name = match.group(1).strip()
            if not metric_name:
                continue
            metrics[metric_name] = _number(match.group(2))

        if metrics:
            relationships[str(character_id)] = metrics

    return {"relationships": deepcopy(relationships)} if relationships else {}
