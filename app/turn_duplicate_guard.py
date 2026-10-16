from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict

from . import session_documents, storage
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
    turns = storage._read_turns_backwards(root, count=1)
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


REQUEST_INDEX_FILE = ".runtime/request_id_index.json"
_REQUEST_INDEX_VERSION = 1


def _turn_digest(turn: Dict[str, Any] | None) -> str:
    if not turn:
        return ""
    serialized = json.dumps(turn, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _request_index(root) -> Dict[str, Any]:
    """An index of stable request IDs into the immutable turn archive.

    A previous rollback, external archive edit or migrated pre-index session
    triggers reconstruction from the authoritative JSONL, not guesswork.
    """
    path = session_documents.file_path(root, "turns.jsonl")
    latest = storage._read_turns_backwards(root, count=1)
    tail = latest[-1] if latest else None
    end_offset = path.stat().st_size if path.exists() else 0
    cached = storage._read_json(root / REQUEST_INDEX_FILE, {})
    last_number = int(tail.get("turn_number", 0) or 0) if tail else 0
    if (
        isinstance(cached, dict)
        and cached.get("version") == _REQUEST_INDEX_VERSION
        and cached.get("archive_size") == end_offset
        and cached.get("last_turn") == last_number
        and cached.get("tail_digest") == _turn_digest(tail)
        and isinstance(cached.get("requests"), dict)
    ):
        return cached

    requests: Dict[str, Dict[str, Any]] = {}
    if path.exists():
        with path.open("rb") as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                row = json.loads(line)
                identity = str(row.get("request_id") or "").strip()
                if identity:
                    requests[identity] = {"byte_offset": offset, "turn_number": row.get("turn_number")}
    index = {
        "version": _REQUEST_INDEX_VERSION,
        "requests": requests,
        "archive_size": end_offset,
        "last_turn": last_number,
        "tail_digest": _turn_digest(tail),
    }
    storage._write_json(root / REQUEST_INDEX_FILE, index)
    return index


def next_request_index(root, turn: Dict[str, Any], encoded_turn: str) -> Dict[str, Any]:
    """Advance the verified index in the turn's atomic transaction."""
    old = _request_index(root)
    archive_path = session_documents.file_path(root, "turns.jsonl")
    end_offset = archive_path.stat().st_size if archive_path.exists() else 0
    requests = dict(old["requests"])
    identity = str(turn.get("request_id") or "").strip()
    if identity:
        requests[identity] = {
            "byte_offset": end_offset,
            "turn_number": int(turn.get("turn_number", 0) or 0),
        }
    return {
        "version": _REQUEST_INDEX_VERSION,
        "requests": requests,
        "archive_size": end_offset + len(encoded_turn.encode("utf-8")),
        "last_turn": int(turn.get("turn_number", 0) or 0),
        "tail_digest": _turn_digest(turn),
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

    index = _request_index(root)
    item = index["requests"].get(identity)
    if not isinstance(item, dict):
        return None
    path = session_documents.file_path(root, "turns.jsonl")
    with path.open("rb") as handle:
        handle.seek(int(item["byte_offset"]))
        row = json.loads(handle.readline())
    if str(row.get("request_id") or "") != identity:
        raise RuntimeError("TURN_REQUEST_INDEX_STALE")
    saved_input = str(row.get("user_input") or "")
    if saved_input != str(user_input):
        raise RuntimeError("TURN_REQUEST_ID_REUSED")
    return {
        "turn_number": int(row.get("turn_number", 0) or 0),
        "turn_id": turn_identity(row),
        "request_id": identity,
        "user_input": saved_input,
        "scene_output": str(row.get("scene_output") or ""),
        "saved_at": str(row.get("saved_at") or ""),
        "duplicate_guard": "request_id",
    }


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
