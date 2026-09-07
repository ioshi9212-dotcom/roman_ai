from __future__ import annotations

from typing import Any, Dict

from . import session_runtime


_ORIGINAL_CONTINUE_SESSION = None
_HEAVY_RESUME_FIELDS = ("chronology_context", "relationships", "relationship_documents")


def _size(value: Any) -> int:
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 0


def _continue_session(session_id: str) -> Dict[str, Any]:
    result = dict(_ORIGINAL_CONTINUE_SESSION(session_id))
    result["resume_payload_counts"] = {
        field: _size(result.get(field)) for field in _HEAVY_RESUME_FIELDS
    }
    for field in _HEAVY_RESUME_FIELDS:
        result.pop(field, None)
    result["resume_payload_compact"] = True
    if not result.get("current_recovery_required"):
        result["instruction"] = (
            "Continue this exact existing session. The resume response is intentionally compact and does not contain the full chronology or relationship stores. "
            "Nothing was deleted or truncated in persistent storage. On the next gameplay input call prepareTurn for this same session_id; prepareTurn reloads the full scene-scoped working context, personal memories, registry, relationships and chronology from persistent storage."
        )
    return result


def install() -> None:
    global _ORIGINAL_CONTINUE_SESSION
    if _ORIGINAL_CONTINUE_SESSION is not None:
        return
    _ORIGINAL_CONTINUE_SESSION = session_runtime.continue_session
    session_runtime.continue_session = _continue_session
