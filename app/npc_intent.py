from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List


MAX_ACTIVE_INTENTS_PER_CHARACTER = 8
MAX_INTENT_SUMMARY_CHARS = 500
_TERMINAL = {"resolved", "closed", "abandoned", "cancelled", "canceled", "superseded", "done"}


def _text(value: Any, limit: int = MAX_INTENT_SUMMARY_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _turn(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _game_day(state: Dict[str, Any]) -> int:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    return max(1, _turn(current.get("game_day") or 1))


def _priority(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, min(100, int(value)))
    lookup = {"low": 25, "normal": 50, "medium": 50, "high": 75, "critical": 95}
    return lookup.get(str(value or "normal").casefold(), 50)


def normalise_intent(raw: Any, *, character_id: str, current_turn: int, current_game_day: int) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    intent_id = _text(raw.get("intent_id") or raw.get("id"), 120)
    summary = _text(raw.get("summary") or raw.get("intent") or raw.get("goal"))
    if not intent_id or not summary:
        return None
    status = _text(raw.get("status") or "active", 40).casefold()
    result: Dict[str, Any] = {
        "intent_id": intent_id,
        "character_id": character_id,
        "kind": _text(raw.get("kind") or "follow_up", 60),
        "summary": summary,
        "status": status,
        "priority": _priority(raw.get("priority")),
        "created_turn": _turn(raw.get("created_turn")) or current_turn,
        "created_game_day": _turn(raw.get("created_game_day")) or current_game_day,
        "last_pursued_turn": _turn(raw.get("last_pursued_turn")),
        "last_pursued_game_day": _turn(raw.get("last_pursued_game_day")),
        "attempt_count": _turn(raw.get("attempt_count")),
        "last_outcome": _text(raw.get("last_outcome")),
        "next_eligible_game_day": _turn(raw.get("next_eligible_game_day")),
        "target_character_id": _text(raw.get("target_character_id"), 120),
        "trigger": _text(raw.get("trigger")),
        "why_it_matters": _text(raw.get("why_it_matters")),
        "planned_action": _text(raw.get("planned_action")),
        "source_fact_ids": [
            _text(value, 120)
            for value in raw.get("source_fact_ids", [])
            if value not in (None, "")
        ][:12] if isinstance(raw.get("source_fact_ids"), list) else [],
    }
    return {key: value for key, value in result.items() if value not in ("", [], None)}


def normalise_store(state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    raw = state.get("npc_intents") if isinstance(state.get("npc_intents"), dict) else {}
    current_turn = _turn(state.get("turn_number"))
    current_game_day = _game_day(state)
    result: Dict[str, List[Dict[str, Any]]] = {}
    for character_id, values in raw.items():
        if not isinstance(values, list):
            continue
        bucket: List[Dict[str, Any]] = []
        seen = set()
        for value in values:
            item = normalise_intent(value, character_id=str(character_id), current_turn=current_turn, current_game_day=current_game_day)
            if item is None or item["intent_id"] in seen:
                continue
            seen.add(item["intent_id"])
            bucket.append(item)
        if bucket:
            result[str(character_id)] = bucket
    return result


def apply_updates(state: Dict[str, Any], updates: Any, *, current_turn: int) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    store = normalise_store(result)
    current_game_day = _game_day(result)
    for raw in updates if isinstance(updates, list) else []:
        if not isinstance(raw, dict):
            continue
        character_id = _text(raw.get("character_id"), 120)
        intent_id = _text(raw.get("intent_id") or raw.get("id"), 120)
        if not character_id or not intent_id:
            continue
        bucket = store.setdefault(character_id, [])
        existing = next((item for item in bucket if item.get("intent_id") == intent_id), None)
        operation = _text(raw.get("operation") or raw.get("action") or "upsert", 30).casefold()
        if operation in {"resolve", "resolved", "close", "closed", "abandon", "abandoned", "cancel", "cancelled", "canceled", "supersede", "superseded"}:
            if existing is None:
                continue
            resolution = _text(raw.get("resolution"))
            if not resolution:
                # A terminal operation without a concrete outcome must not silently erase an NPC-owned motive.
                if raw.get("pursued_now") is True:
                    existing["last_pursued_turn"] = current_turn
                    existing["last_pursued_game_day"] = current_game_day
                    existing["attempt_count"] = _turn(existing.get("attempt_count")) + 1
                    existing["last_outcome"] = _text(raw.get("last_outcome") or "attempted_without_resolution")
                continue
            existing["status"] = "resolved" if operation.startswith(("resolv", "clos")) else "abandoned"
            existing["resolved_turn"] = current_turn
            existing["resolved_game_day"] = current_game_day
            existing["resolution"] = resolution
            continue
        base = deepcopy(existing) if existing is not None else {"intent_id": intent_id, "character_id": character_id}
        base.update({key: deepcopy(value) for key, value in raw.items() if key not in {"operation", "action"}})
        if raw.get("pursued_now") is True:
            base["last_pursued_turn"] = current_turn
            base["last_pursued_game_day"] = current_game_day
            base["attempt_count"] = _turn(base.get("attempt_count")) + 1
            base["last_outcome"] = _text(raw.get("last_outcome") or "attempted_without_resolution")
        item = normalise_intent(base, character_id=character_id, current_turn=current_turn, current_game_day=current_game_day)
        if item is None:
            continue
        if existing is None:
            bucket.append(item)
        else:
            existing.clear()
            existing.update(item)
    result["npc_intents"] = store
    return result


def active_intents_for(
    state: Dict[str, Any],
    character_ids: Iterable[str],
    *,
    current_turn: int,
    max_per_character: int = MAX_ACTIVE_INTENTS_PER_CHARACTER,
) -> Dict[str, List[Dict[str, Any]]]:
    store = normalise_store(state)
    game_day = _game_day(state)
    result: Dict[str, List[Dict[str, Any]]] = {}
    for character_id in dict.fromkeys(str(value) for value in character_ids if value):
        candidates = []
        for item in store.get(character_id, []):
            status = str(item.get("status") or "active").casefold()
            if status in _TERMINAL:
                continue
            next_day = _turn(item.get("next_eligible_game_day"))
            due = next_day == 0 or game_day >= next_day
            last_turn = _turn(item.get("last_pursued_turn"))
            age = max(0, current_turn - last_turn) if last_turn else max(0, current_turn - _turn(item.get("created_turn")))
            scored = deepcopy(item)
            scored["eligible_now"] = due
            scored["turns_since_pursued"] = age
            candidates.append(scored)
        candidates.sort(
            key=lambda item: (
                bool(item.get("eligible_now")),
                _priority(item.get("priority")),
                int(item.get("turns_since_pursued") or 0),
                -_turn(item.get("created_turn")),
            ),
            reverse=True,
        )
        if candidates:
            result[character_id] = candidates[:max_per_character]
    return result
