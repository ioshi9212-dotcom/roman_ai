"""Safe, non-canonical diagnostics for rejected gameplay/audit commits.

A ChatGPT Action may display only ClientResponseError for HTTP 409. Preserve the
validation code for resumeSession without changing the canonical turn archive.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import storage

_LOG = logging.getLogger(__name__)
_FILE = "last_commit_rejection.json"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,99}$")


def _safe_code(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if _CODE.fullmatch(candidate) else "COMMIT_REJECTED"


def _safe_detail(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        value = dict(detail)
        code = _safe_code(value.get("code"))
        message = str(value.get("message") or value.get("instruction") or "")
        result: dict[str, Any] = {"code": code, "message": message[:600]}
        for name in ("required_character_ids", "missing_character_ids", "unexpected_character_ids",
                     "unread_chunk_indices", "stagnant_turns_before_this_commit",
                     "world_stagnant_turns_before_this_commit"):
            val = value.get(name)
            if isinstance(val, list):
                result[name] = [str(x)[:90] for x in val[:25]]
            elif isinstance(val, int) and not isinstance(val, bool):
                result[name] = val
        return result
    text = str(detail or "").strip()
    code = _safe_code(text)
    return {"code": code, "message": "" if code == text else text[:600]}


def record(session_id: str, *, operation: str, identity: str, status_code: int, detail: Any) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        return
    safe = _safe_detail(detail)
    meta = storage._read_json(root / "meta.json", {})
    report = {
        "operation": operation,
        "identity": str(identity or "")[:128],
        "status_code": int(status_code),
        "code": safe.pop("code"),
        "message": safe.pop("message"),
        "detail": safe,
        "turn_number_at_failure": int(meta.get("turn_number", 0) or 0),
        "audit_required_at_failure": bool(meta.get("audit_required")),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "committed": False,
        "instruction": (
            "This is a rejected commit, not a saved scene. Correct the named validation error "
            "and retry the SAME pending packet once; read pending chunks first if needed. "
            "Never invent a new scene or roll back a committed turn to resolve a commit rejection."
        ),
    }
    try:
        storage._write_json(root / _FILE, report)
    except OSError:
        _LOG.exception("Unable to store commit diagnostic session=%s", session_id)


def latest(session_id: str) -> dict[str, Any] | None:
    root = storage.SESSIONS_DIR / session_id
    report = storage._read_json(root / _FILE, {})
    if not isinstance(report, dict) or not report.get("code"):
        return None
    meta = storage._read_json(root / "meta.json", {})
    # A successful turn or audit makes the previous refusal obsolete.
    if int(meta.get("turn_number", 0) or 0) != report.get("turn_number_at_failure"):
        return None
    if report.get("operation") == "commitAudit" and not meta.get("audit_required"):
        return None
    return report


def clear(session_id: str) -> None:
    (storage.SESSIONS_DIR / session_id / _FILE).unlink(missing_ok=True)
