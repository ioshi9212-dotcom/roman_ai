from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from . import storage
from .operation_receipts import turn_identity


RECENT_DUPLICATE_WINDOW_SECONDS = 180


def _saved_at_seconds(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def recent_duplicate_turn(session_id: str, user_input: str) -> Dict[str, Any] | None:
    """Legacy compatibility only. New clients should use request_id."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    turns = storage._read_turns(root)
    if not turns:
        return None
    last = turns[-1]
    if str(last.get("user_input") or "") != str(user_input):
        return None
    saved = _saved_at_seconds(last.get("saved_at"))
    if saved is None:
        return None
    age = datetime.now(timezone.utc).timestamp() - saved
    if age < 0 or age > RECENT_DUPLICATE_WINDOW_SECONDS:
        return None
    return {
        "turn_number": int(last.get("turn_number", 0) or 0),
        "turn_id": turn_identity(last),
        "user_input": str(last.get("user_input") or ""),
        "scene_output": str(last.get("scene_output") or ""),
        "saved_at": str(last.get("saved_at") or ""),
        "age_seconds": max(0, int(age)),
        "duplicate_guard": "legacy_text_window",
    }


def committed_request_turn(
    session_id: str,
    request_id: str,
    user_input: str,
) -> Dict[str, Any] | None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    identity = str(request_id or "").strip()
    if not identity:
        return None

    for turn in reversed(storage._read_turns(root)):
        if str(turn.get("request_id") or "") != identity:
            continue
        saved_input = str(turn.get("user_input") or "")
        if saved_input != str(user_input):
            raise RuntimeError("TURN_REQUEST_ID_REUSED")
        return {
            "turn_number": int(turn.get("turn_number", 0) or 0),
            "turn_id": turn_identity(turn),
            "request_id": identity,
            "user_input": saved_input,
            "scene_output": str(turn.get("scene_output") or ""),
            "saved_at": str(turn.get("saved_at") or ""),
            "duplicate_guard": "request_id",
        }
    return None


def duplicate_prepare_response(row: Dict[str, Any]) -> Dict[str, Any]:
    request_id = str(row.get("request_id") or "").strip()
    request_based = bool(request_id)
    result = {
        "ok": True,
        "already_committed_duplicate": True,
        "duplicate_guard": "request_id" if request_based else True,
        "duplicate_guard_mode": "request_id" if request_based else row.get("duplicate_guard", "legacy_text_window"),
        "turn_number": int(row.get("turn_number", 0) or 0),
        "current_turn_id": row.get("turn_id"),
        "saved_at": row.get("saved_at"),
        "scene_output": row.get("scene_output", ""),
    }
    if request_based:
        result["request_id"] = request_id
        result["instruction"] = (
            "This request_id already committed successfully. Return the saved scene_output; do not create another turn. "
            "An identical user_input with a NEW request_id is a legitimate new gameplay turn."
        )
    else:
        result["instruction"] = (
            "Legacy client retry guard: this exact user_input was committed moments ago. Return the saved scene_output. "
            "Upgrade prepareTurn to request_id semantics to distinguish intentional repeated text from technical retries."
        )
    return result
