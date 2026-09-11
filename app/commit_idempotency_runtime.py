from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict

from . import session_runtime, storage


_ORIGINAL_SESSION_COMMIT = None
_ORIGINAL_STORAGE_COMMIT = None
_FINGERPRINT_FIELD = "_commit_request_fingerprint"


def _request_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _duplicate_result(session_id: str, fingerprint: str) -> Dict[str, Any] | None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0)
    if current_turn <= 0:
        return None
    turns = storage._read_turns(root)
    if not turns:
        return None
    last = turns[-1] if isinstance(turns[-1], dict) else {}
    if int(last.get("turn_number", 0) or 0) != current_turn:
        return None
    extracted = last.get("extracted") if isinstance(last.get("extracted"), dict) else {}
    if extracted.get(_FINGERPRINT_FIELD) != fingerprint:
        return None
    audit_due = current_turn % 15 == 0
    return {
        "ok": True,
        "turn_number": current_turn,
        "audit_due": audit_due,
        "audit_range": [max(1, current_turn - 14), current_turn] if audit_due else None,
        "handoff_required": bool(meta.get("handoff_required")),
        "already_committed": True,
        "idempotent_replay": True,
    }


def _storage_commit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fingerprint = str(payload.get(_FINGERPRINT_FIELD) or "")
    if not fingerprint:
        return _ORIGINAL_STORAGE_COMMIT(session_id, payload)
    duplicate = _duplicate_result(session_id, fingerprint)
    if duplicate is not None:
        return duplicate

    prepared = deepcopy(payload)
    extracted = prepared.get("extracted") if isinstance(prepared.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)
    extracted[_FINGERPRINT_FIELD] = fingerprint
    prepared["extracted"] = extracted
    return _ORIGINAL_STORAGE_COMMIT(session_id, prepared)


def _session_commit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fingerprint = _request_fingerprint(payload)
    duplicate = _duplicate_result(session_id, fingerprint)
    if duplicate is not None:
        return duplicate

    prepared = deepcopy(payload)
    prepared[_FINGERPRINT_FIELD] = fingerprint
    result = dict(_ORIGINAL_SESSION_COMMIT(session_id, prepared))
    result.setdefault("already_committed", False)
    result.setdefault("idempotent_replay", False)
    return result


def install() -> None:
    global _ORIGINAL_SESSION_COMMIT, _ORIGINAL_STORAGE_COMMIT
    if _ORIGINAL_SESSION_COMMIT is not None:
        return
    _ORIGINAL_SESSION_COMMIT = session_runtime.commit_turn
    _ORIGINAL_STORAGE_COMMIT = storage.commit_turn
    storage.commit_turn = _storage_commit
    session_runtime.commit_turn = _session_commit
