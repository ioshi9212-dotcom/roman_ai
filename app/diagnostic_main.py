import json
import os

from fastapi import HTTPException

from .context_stats import session_context_stats
from .main import app


@app.get("/sessions/{session_id}/context-stats", operation_id="getSessionContextStats")
def session_context_stats_get(session_id: str):
    try:
        return session_context_stats(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


def _log_stats(session_id: str) -> None:
    stats = session_context_stats(session_id)
    summary = {
        "session_id": stats["session_id"],
        "turn_number": stats["turn_number"],
        "known_session_bytes": stats["known_session_bytes"],
        "files_bytes": stats["files_bytes"],
        "json_chars": stats["json_chars"],
        "chronology": stats["chronology"],
        "memory_chars": stats["memory"]["chars"],
        "memory_characters": stats["memory"]["characters"],
    }
    print("CONTEXT_STATS_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    for character_id, row in stats["memory"]["by_character"].items():
        print(
            "CONTEXT_STATS_MEMORY "
            + json.dumps({"character_id": character_id, **row}, ensure_ascii=False),
            flush=True,
        )


_target_session = os.getenv("DIAGNOSTIC_SESSION_ID", "").strip()
if _target_session:
    try:
        _log_stats(_target_session)
    except FileNotFoundError:
        print("CONTEXT_STATS_ERROR session_not_found " + _target_session, flush=True)
