"""Causal plot momentum in the single live turn pipeline.

This module does not invent future events or infer NPC knowledge. It makes
existing persistent threads/intents actionable and refuses to treat decorative
conversation, scene-progress flags or random arrivals as a plot milestone.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException

from . import npc_intent, storage, story_thread


PLOT_STAGNATION_LIMIT = 4
_TERMINAL = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned", "superseded"}


def _rows(value: Any) -> list[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _has_text(raw: Dict[str, Any], *keys: str) -> bool:
    return any(isinstance(raw.get(key), str) and bool(raw[key].strip()) for key in keys)


def has_plot_consequence(extracted: Any) -> bool:
    """A persisted consequence, not mere motion or an author's progress flag.

    Semantic truth remains the writer's responsibility. A valid log entry alone
    cannot prove a scene had dramatic value, but scene_progressed, relationship
    deltas, entrances and location/time changes are never sufficient.
    """
    if not isinstance(extracted, dict):
        return False

    if any(_has_text(row, "event", "summary", "description", "text")
           for row in _rows(extracted.get("chronology"))):
        return True

    for row in _rows(extracted.get("story_thread_updates")):
        op = str(row.get("operation") or "upsert").casefold().strip()
        if op in {"resolve", "abandon"} and _has_text(row, "resolution", "progress_summary"):
            return True
        if op == "upsert":
            # A newly established causal line is a development; a bare name is not.
            created = (
                _has_text(row, "premise", "summary")
                and (
                    _has_text(row, "current_goal", "current_phase", "progress_summary")
                    or bool(row.get("unresolved"))
                    or bool(row.get("advance_conditions"))
                )
            )
            advanced = row.get("progressed_now") is True and (
                _has_text(row, "progress_summary", "current_phase")
                or bool(row.get("anchor_facts"))
            )
            if created or advanced:
                return True

    for row in _rows(extracted.get("npc_intent_updates")):
        op = str(row.get("operation") or "upsert").casefold().strip()
        if op in {"resolve", "abandon"} and _has_text(row, "resolution"):
            return True
        if row.get("pursued_now") is True and _has_text(row, "last_outcome"):
            if str(row["last_outcome"]).strip().casefold() != "attempted_without_resolution":
                return True

    # An enduring world consequence is valid; scene presence, clock and place
    # are not. The log must explicitly connect such a change to a story field.
    patch = extracted.get("state_patch")
    world = patch.get("world") if isinstance(patch, dict) else None
    if isinstance(world, dict):
        story_keys = ("story_pillars", "plot", "open_cases", "consequences", "conflicts")
        if any(world.get(key) not in (None, "", [], {}) for key in story_keys):
            return True
    return False


def trailing_plot_stagnant_turns(root: Path, *, limit: int = 16) -> int:
    count = 0
    for turn in reversed(storage._read_turns(root)[-limit:]):
        if not isinstance(turn, dict) or has_plot_consequence(turn.get("extracted")):
            break
        count += 1
    return count


def _persisted_intents(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    for cid, values in npc_intent.normalise_store(state).items():
        for item in values:
            if str(item.get("status") or "active").casefold() in _TERMINAL:
                continue
            rows.append({
                "character_id": cid,
                "intent_id": item.get("intent_id"),
                "summary": item.get("summary"),
                "planned_action": item.get("planned_action"),
                "trigger": item.get("trigger"),
                "next_eligible_game_day": item.get("next_eligible_game_day"),
                "priority": item.get("priority", 50),
            })
    rows.sort(key=lambda row: int(row.get("priority") or 0), reverse=True)
    return rows


def plot_packet(root: Path, state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Small, non-secret-revealing director instructions for each real prepare."""
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    day = current.get("game_day") or 1
    try:
        day = int(day)
    except (ValueError, TypeError):
        day = 1
    persisted = story_thread.active_threads(state)
    intents = _persisted_intents(state)
    due = [row for row in intents if not row.get("next_eligible_game_day")
           or int(row["next_eligible_game_day"]) <= day]
    streak = trailing_plot_stagnant_turns(root)
    cast = context.get("cast_registry") if isinstance(context.get("cast_registry"), dict) else {}
    cast_rows = cast.get("characters") if isinstance(cast.get("characters"), list) else []
    candidates = [
        {"character_id": row.get("character_id"), "story_function": row.get("story_function"),
         "goals": row.get("goals")}
        for row in cast_rows
        if isinstance(row, dict) and not row.get("is_pov")
        and (row.get("story_function") or row.get("goals"))
    ][:6]
    return {
        "mandatory": True,
        "plot_stagnant_turns": streak,
        "progress_due": streak >= 2,
        "must_change_causal_situation_this_turn": streak >= PLOT_STAGNATION_LIMIT,
        "saved_active_thread_count": len(persisted),
        "saved_active_intent_count": len(intents),
        "due_npc_intents": deepcopy(due[:6]),
        "canon_character_drivers": candidates if not persisted and not intents else [],
        "open_threads": [
            {"thread_id": tid, "current_phase": data.get("current_phase"),
             "current_goal": data.get("current_goal"),
             "current_circumstances": data.get("current_circumstances"),
             "next_possible_events": data.get("next_possible_events"),
             "advance_conditions": data.get("advance_conditions")}
            for tid, data in list(persisted.items())[:8]
        ],
        "rule": (
            "ВЫБОР POV НЕ УПРАВЛЯЕТ МИРОМ. NPC самостоятельно преследуют конкретные цели, "
            "пробуют способы, сталкиваются с препятствиями и меняют планы без команд POV. "
            "Продвигай незакрытые линии по причинности; события за кадром выдавай только через "
            "доступные POV последствия, без всеведения и без телепортации. "
            "Если в каноне есть значимые незакрытые линии или в сцене возникла новая такая линия, "
            "сохраняй её в story_thread_updates (этап, участники, обстоятельства, ближайшие "
            "возможные события, условия продвижения). Сохраняй конкретные цели NPC через "
            "npc_intent_updates; реальные действия, исходы и изменения должны переживать следующий ход. "
            "Пустые active_threads/npc_intents — НЕ свидетельство отсутствия сюжета: восстанови "
            "только подтверждённые каноном или сыгранными событиями незакрытые линии, не сочиняй "
            "несостоявшиеся события задним числом. Не выдавай новую болтовню, случайного гостя, "
            "декорацию, грозу или перемещение за развитие. Быт допускается, но не должен вытеснять "
            "сюжет. Не делай искусственную катастрофу или принудительное раскрытие тайны ради темпа. "
            "Если must_change_causal_situation_this_turn=true, нужен обоснованный результат: "
            "новая информация, реальное решение/препятствие, действие NPC с последствиями, "
            "изменение этапа линии или обстоятельств; сохрани факт в chronology/thread/intent. "
            "Не создавай фиктивные записи ради прохождения проверки."
        ),
    }


def validate_plot_momentum(root: Path, payload: Dict[str, Any]) -> None:
    streak = trailing_plot_stagnant_turns(root)
    if streak < PLOT_STAGNATION_LIMIT:
        return
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    if has_plot_consequence(extracted):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "PLOT_MOMENTUM_REQUIRED",
            "message": (
                "Four or more consecutive turns lack a persisted causal development. "
                "Continue the scene with a real, canon-consistent consequence and save "
                "its chronology/story_thread_updates/npc_intent_updates. A new visitor, "
                "a relationship delta, a new pending plan or scene_progressed=true alone "
                "does not count. Do not fabricate a disaster or overwrite NPC autonomy."
            ),
            "plot_stagnant_turns_before_commit": streak,
        },
    )
