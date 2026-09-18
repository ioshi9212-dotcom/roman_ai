from __future__ import annotations

from typing import Any, Dict

from . import storage
from .operation_receipts import turn_identity


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
        raise RuntimeError("TURN_REQUEST_ID_REQUIRED")

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
        }
    return None


def duplicate_prepare_response(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "already_committed_duplicate": True,
        "duplicate_guard": "request_id",
        "request_id": row.get("request_id"),
        "turn_number": int(row.get("turn_number", 0) or 0),
        "current_turn_id": row.get("turn_id"),
        "saved_at": row.get("saved_at"),
        "scene_output": row.get("scene_output", ""),
        "instruction": (
            "This request_id already completed successfully. Do not create another turn for this request. "
            "Return the saved scene_output. Identical user_input with a new request_id is a legitimate new gameplay turn."
        ),
    }
