from __future__ import annotations

from typing import Any, Dict

from . import session_runtime, storage


_ORIGINAL_CONTINUE_SESSION = None
_HEAVY_RESUME_FIELDS = ("chronology_context", "relationships", "relationship_documents")


def _size(value: Any) -> int:
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 0


def _pending_turn(root) -> Dict[str, Any] | None:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet.get("packet_id"):
        return None
    chunks = packet.get("chunks") if isinstance(packet.get("chunks"), list) else []
    read = sorted({
        int(value)
        for value in packet.get("read_chunks", [])
        if isinstance(value, int) and 0 <= int(value) < len(chunks)
    })
    unread = [index for index in range(len(chunks)) if index not in set(read)]
    return {
        "packet_id": str(packet.get("packet_id")),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "request_id": str(packet.get("request_id") or "") or None,
        "user_input": str(packet.get("user_input") or ""),
        "chunk_count": len(chunks),
        "read_chunks": read,
        "unread_chunk_indices": unread,
        "ready_for_commit": not unread,
    }


def _last_committed_turn(root) -> Dict[str, Any] | None:
    turns = storage._read_turns(root)
    if not turns:
        return None
    last = turns[-1]
    if not isinstance(last, dict):
        return None
    return {
        "turn_number": int(last.get("turn_number", 0) or 0),
        "scene_output": str(last.get("scene_output") or ""),
    }


def _continue_session(session_id: str) -> Dict[str, Any]:
    result = dict(_ORIGINAL_CONTINUE_SESSION(session_id))
    result["resume_payload_counts"] = {
        field: _size(result.get(field)) for field in _HEAVY_RESUME_FIELDS
    }
    for field in _HEAVY_RESUME_FIELDS:
        result.pop(field, None)
    result["resume_payload_compact"] = True
    root = storage.SESSIONS_DIR / session_id
    result["last_committed_turn"] = _last_committed_turn(root)
    pending = _pending_turn(root)
    if pending:
        result["pending_turn"] = pending
    if result.get("current_recovery_required"):
        if pending:
            result["pending_turn_before_current_recovery"] = pending
    elif pending:
        result["instruction"] = (
            "An uncommitted turn packet already exists. last_committed_turn.scene_output is the exact latest committed scene. "
            "Do not start or replace another gameplay turn. Reuse pending_turn with the same request_id, read only unread_chunk_indices, then commit once."
        )
    else:
        result["instruction"] = (
            "Continue this exact existing session. last_committed_turn.scene_output is the exact latest saved scene and may be shown verbatim when the user asks for the last scene. "
            "The resume response stays compact; full canon remains in persistent storage. On the next gameplay input call prepareTurn for this same session_id."
        )
    return result


def install() -> None:
    global _ORIGINAL_CONTINUE_SESSION
    if _ORIGINAL_CONTINUE_SESSION is not None:
        return
    _ORIGINAL_CONTINUE_SESSION = session_runtime.continue_session
    session_runtime.continue_session = _continue_session
