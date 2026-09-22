from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage


DEFAULT_SOURCE = "https://romanai-production-0fdb.up.railway.app"
ENV_SESSION_ID = "ROMAN_MIGRATE_SESSION_ID"
ENV_SOURCE = "ROMAN_MIGRATE_SOURCE"
STATUS_FILE = "migration_status.json"


def _get_json(url: str, *, timeout: int = 60) -> Any:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "roman-ai-startup-migrator/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"GET {url} returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} returned HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc.reason}") from exc


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _fetch_turns(source: str, session_id: str, last_turn: int, *, batch_size: int = 50) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for start in range(1, last_turn + 1, batch_size):
        end = min(last_turn, start + batch_size - 1)
        query = urllib.parse.urlencode({"start_turn": start, "end_turn": end})
        payload = _get_json(f"{source.rstrip('/')}/sessions/{session_id}/turns?{query}")
        if isinstance(payload, dict) and isinstance(payload.get("turns"), list):
            batch = payload["turns"]
        elif isinstance(payload, list):
            batch = payload
        else:
            raise RuntimeError(
                f"Unexpected turn-range payload for {start}-{end}: {type(payload).__name__}"
            )
        turns.extend(item for item in batch if isinstance(item, dict))
    turns.sort(key=lambda item: int(item.get("turn_number", 0) or 0))
    return turns


def _validate(session_id: str, session: dict[str, Any], turns: list[dict[str, Any]]) -> int:
    meta = session.get("meta")
    if not isinstance(meta, dict):
        raise RuntimeError("Source session payload has no meta object.")
    last_turn = int(meta.get("turn_number", 0) or 0)
    numbers = [int(item.get("turn_number", 0) or 0) for item in turns]
    if last_turn:
        expected = list(range(1, last_turn + 1))
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            duplicate_count = len(numbers) - len(set(numbers))
            raise RuntimeError(
                f"Turn archive is not contiguous. Missing={missing[:20]} duplicates={duplicate_count}; "
                f"highest={numbers[-1] if numbers else 0}, expected={last_turn}."
            )
    for key in ("source", "characters", "state", "memory", "chronology", "scene_history"):
        if key not in session:
            raise RuntimeError(f"Source session payload is missing {key}.")
    returned_id = meta.get("session_id")
    if returned_id and str(returned_id) != session_id:
        raise RuntimeError(f"Source returned a different session_id: {returned_id}")
    return last_turn


def _write_session(target: Path, session: dict[str, Any], turns: list[dict[str, Any]]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    mapping = {
        "meta.json": session["meta"],
        "source.json": session["source"],
        "characters.json": session["characters"],
        "state.json": session["state"],
        "memory.json": session["memory"],
        "chronology.json": session["chronology"],
        "scene_memory.json": session["scene_history"],
    }
    for filename, value in mapping.items():
        _atomic_json(target / filename, value)

    fd, tmp_name = tempfile.mkstemp(prefix="turns.", suffix=".tmp", dir=str(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in turns:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_name, target / "turns.jsonl")
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

    _atomic_json(target / "handoff_tail.json", turns[-6:])
    for transient in ("turn_packet.json", "resume_token.json", "pending_operation.json"):
        path = target / transient
        if path.exists():
            path.unlink()


def _status_path() -> Path:
    return storage.DATA_DIR / STATUS_FILE


def read_migration_status() -> dict[str, Any]:
    path = _status_path()
    if not path.exists():
        return {"configured": bool(os.getenv(ENV_SESSION_ID)), "status": "not_run"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"configured": bool(os.getenv(ENV_SESSION_ID)), "status": "unreadable"}
    if isinstance(data, dict):
        data.setdefault("configured", bool(os.getenv(ENV_SESSION_ID)))
        return data
    return {"configured": bool(os.getenv(ENV_SESSION_ID)), "status": "invalid"}


def run_startup_session_migration() -> dict[str, Any]:
    session_id = str(os.getenv(ENV_SESSION_ID) or "").strip()
    if not session_id:
        return {"configured": False, "status": "disabled"}

    source = str(os.getenv(ENV_SOURCE) or DEFAULT_SOURCE).rstrip("/")
    target = storage.SESSIONS_DIR / session_id
    if target.exists():
        meta = storage._read_json(target / "meta.json", {})
        result = {
            "configured": True,
            "status": "already_present",
            "session_id": session_id,
            "turn_number": int(meta.get("turn_number", 0) or 0) if isinstance(meta, dict) else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(_status_path(), result)
        return result

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        session = _get_json(f"{source}/sessions/{session_id}")
        if not isinstance(session, dict):
            raise RuntimeError(f"Unexpected session payload: {type(session).__name__}")
        meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
        last_turn = int(meta.get("turn_number", 0) or 0)
        turns = _fetch_turns(source, session_id, last_turn) if last_turn else []
        _validate(session_id, session, turns)

        storage.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        staging = storage.SESSIONS_DIR / f".{session_id}.migration"
        if staging.exists():
            shutil.rmtree(staging)
        _write_session(staging, session, turns)
        staging.replace(target)

        result = {
            "configured": True,
            "status": "success",
            "session_id": session_id,
            "turn_number": last_turn,
            "turns_downloaded": len(turns),
            "source": source,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(_status_path(), result)
        return result
    except Exception as exc:
        result = {
            "configured": True,
            "status": "error",
            "session_id": session_id,
            "source": source,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        _atomic_json(_status_path(), result)
        return result
