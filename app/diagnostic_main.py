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


_target_session = os.getenv("DIAGNOSTIC_SESSION_ID", "").strip()
if _target_session:
    try:
        print("CONTEXT_STATS " + json.dumps(session_context_stats(_target_session), ensure_ascii=False), flush=True)
    except FileNotFoundError:
        print("CONTEXT_STATS_ERROR session_not_found " + _target_session, flush=True)
