"""Compact, advisory scene-engine cues. No save-time validation or forced NPC action.

The existing cast registry and state remain authoritative. A cue only brings a
few eligible alternatives to the writer's attention, not a new plot contract.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _trim(value: Any, width: int = 140) -> str:
    return " ".join(str(value or "").split())[:width]


def build(
    state: Dict[str, Any],
    cast_rows: List[Dict[str, Any]],
    current_turn: int,
) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    unfinished = current.get("unfinished_actions")
    unfinished = unfinished if isinstance(unfinished, list) else []
    # Current-scene threads and unfinished business provide natural next
    # moves, without requiring one of them to advance on *every* turn.
    prompts = [_trim(text, 180) for text in unfinished if _trim(text)][:3]

    raw_threads = state.get("threads")
    threads = list(raw_threads.values()) if isinstance(raw_threads, dict) else raw_threads
    threads = threads if isinstance(threads, list) else []
    active_threads = []
    for row in threads:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "active").casefold() in {
            "resolved", "closed", "done", "abandoned", "cancelled", "canceled",
        }:
            continue
        summary = _trim(row.get("summary") or row.get("title") or row.get("name"))
        if summary:
            active_threads.append(summary)

    offscreen = [
        row for row in cast_rows
        if isinstance(row, dict) and row.get("offscreen_can_initiate") is True
        and row.get("character_id")
    ]
    offscreen.sort(key=lambda row: (
        {"core": 0, "recurring": 1, "support": 2}.get(str(row.get("importance") or ""), 3),
        str(row.get("character_id")),
    ))
    # Rotate attention without making rotation a requirement for appearance.
    # Avoid repeatedly favoring just the first three alphabetic cast members.
    focus = []
    if offscreen:
        offset = max(0, int(current_turn)) % len(offscreen)
        focus = (offscreen[offset:] + offscreen[:offset])[:3]

    scene_npcs = [
        str(row["character_id"])
        for row in cast_rows
        if isinstance(row, dict) and row.get("character_id")
        and row.get("present") is True and row.get("is_pov") is not True
    ]
    return {
        "advisory_only": True,
        "unfinished_actions": prompts,
        "active_threads": active_threads[:2],
        "offscreen_focus_ids": [str(row["character_id"]) for row in focus],
        "physical_relationship_review_ids": scene_npcs[:10],
        "instruction": (
            "Продолжай реальные незавершённые дела и решения персонажей, не обрывая живой разговор. "
            "Offscreen NPC вправе действовать по своим целям без запроса POV и без отдельного intent. "
            "Фокус лишь помогает вспомнить персонажей, не обязывает их появляться или что-то менять. "
            "Отношения пересматривай по случившемуся, не меняй цифры механически. "
            "Никаких дополнительных требований к commitTurn."
        ),
    }
