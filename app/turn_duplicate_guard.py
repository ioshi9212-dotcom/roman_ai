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
    }


def duplicate_prepare_response(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "already_committed_duplicate": True,
        "duplicate_guard": True,
        "turn_number": int(row.get("turn_number", 0) or 0),
        "current_turn_id": row.get("turn_id"),
        "saved_at": row.get("saved_at"),
        "scene_output": row.get("scene_output", ""),
        "instruction": (
            "This exact user_input was already committed moments ago. Do not call prepareTurn or commitTurn again for it. "
            "Return the saved scene_output to the user as the result of that same turn."
        ),
    }
