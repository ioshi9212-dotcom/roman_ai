from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict

from fastapi import HTTPException

from . import session_runtime, storage
from .story_thread import apply_updates


_ORIGINAL_COMMIT_TURN = None
_ORIGINAL_COMMIT_AUDIT = None
_STAGNATION_LIMIT = 3
_RELATIONSHIP_DELTA_RE = re.compile(r"/delta\s*[+-]?\d", re.IGNORECASE)


def _updates_progress(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for raw in values:
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "upsert").casefold().strip()
        if operation in {"resolve", "abandon"}:
            return True
        if raw.get("progressed_now") is True:
            return True
        if operation == "upsert" and raw.get("progressed_now") is not False:
            return True
    return False


def _intent_progress(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for raw in values:
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "upsert").casefold().strip()
        if raw.get("pursued_now") is True or operation in {"resolve", "abandon"}:
            return True
        if operation == "upsert" and (raw.get("summary") or raw.get("planned_action")):
            return True
    return False


def _container_has_progress(container: Any, scene_output: str = "") -> bool:
    if not isinstance(container, dict):
        return False
    if isinstance(container.get("chronology"), list) and container["chronology"]:
        return True
    if _updates_progress(container.get("story_thread_updates")):
        return True
    if _intent_progress(container.get("npc_intent_updates")):
        return True
    for key in ("presence_updates", "relationship_updates", "character_upserts"):
        if isinstance(container.get(key), list) and container[key]:
            return True
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    if isinstance(patch.get("world"), dict) and patch["world"]:
        return True
    return bool(_RELATIONSHIP_DELTA_RE.search(str(scene_output or "")))


def _turn_has_progress(turn: Dict[str, Any]) -> bool:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    return _container_has_progress(extracted, str(turn.get("scene_output") or ""))


def trailing_stagnant_turns(root, *, limit: int = 12) -> int:
    turns = storage._read_turns(root)
    count = 0
    for turn in reversed(turns[-limit:]):
        if not isinstance(turn, dict) or _turn_has_progress(turn):
            break
        count += 1
    return count


def _progress_required_error(streak: int) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "STORY_PROGRESS_REQUIRED",
            "message": (
                "The previous turns have formed a static streak. Do not commit another scene that only extends routine, rest, waiting or neutral talk. "
                "Advance an existing story thread/NPC intent/consequence, bring the scene to a causally grounded event, or time-skip to the next substantive moment. "
                "Record the real movement in chronology and/or story_thread_updates/npc_intent_updates before retrying the same turn."
            ),
            "stagnant_turns_before_this_commit": streak,
            "maximum_consecutive_static_turns": _STAGNATION_LIMIT,
        },
    )


def _with_story_patch(session_id: str, payload: Dict[str, Any], *, audit: bool = False) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0) + (0 if audit else 1)

    container_key = "repairs" if audit else "extracted"
    container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
    updates = container.get("story_thread_updates")

    if not audit:
        streak = trailing_stagnant_turns(root)
        if streak >= _STAGNATION_LIMIT and not _container_has_progress(container, str(result.get("scene_output") or "")):
            _progress_required_error(streak)

    if not isinstance(updates, list) or not updates:
        return result

    updated_state = apply_updates(state, updates, current_turn=current_turn)
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    patch = deepcopy(patch)
    patch["threads"] = deepcopy(updated_state.get("threads", {}))
    container = deepcopy(container)
    container["state_patch"] = patch
    result[container_key] = container
    return result


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_TURN(session_id, _with_story_patch(session_id, payload, audit=False))


def _commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_AUDIT(session_id, _with_story_patch(session_id, payload, audit=True))


def install() -> None:
    global _ORIGINAL_COMMIT_TURN, _ORIGINAL_COMMIT_AUDIT
    if _ORIGINAL_COMMIT_TURN is not None:
        return
    _ORIGINAL_COMMIT_TURN = session_runtime.commit_turn
    _ORIGINAL_COMMIT_AUDIT = session_runtime.commit_audit
    session_runtime.commit_turn = _commit_turn
    session_runtime.commit_audit = _commit_audit
