#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SOURCE = "https://romanai-production-0fdb.up.railway.app"
DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/data")


def _get_json(url: str, *, timeout: int = 60) -> Any:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "roman-ai-session-migrator/1"})
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
        url = f"{source.rstrip('/')}/sessions/{session_id}/turns?{query}"
        batch = _get_json(url)
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected turn-range payload for {start}-{end}: {type(batch).__name__}")
        turns.extend(item for item in batch if isinstance(item, dict))
        print(f"Fetched turns {start}-{end}: {len(batch)}")
    turns.sort(key=lambda item: int(item.get("turn_number", 0) or 0))
    return turns


def _validate_bundle(session_id: str, session: dict[str, Any], turns: list[dict[str, Any]]) -> int:
    meta = session.get("meta")
    if not isinstance(meta, dict):
        raise RuntimeError("Source session payload has no meta object.")
    last_turn = int(meta.get("turn_number", 0) or 0)
    if last_turn < 0:
        raise RuntimeError("Invalid turn_number in source meta.")
    if last_turn:
        numbers = [int(item.get("turn_number", 0) or 0) for item in turns]
        if not numbers or numbers[-1] != last_turn:
            raise RuntimeError(
                f"Turn archive is incomplete: source meta says {last_turn}, "
                f"but highest downloaded turn is {numbers[-1] if numbers else 0}."
            )
        expected = list(range(1, last_turn + 1))
        if numbers != expected:
            missing = sorted(set(expected) - set(numbers))
            duplicate_count = len(numbers) - len(set(numbers))
            raise RuntimeError(
                f"Turn archive is not contiguous. Missing={missing[:20]} "
                f"duplicates={duplicate_count}."
            )
    for key in ("source", "characters", "state", "memory", "chronology", "scene_history"):
        if key not in session:
            raise RuntimeError(f"Source session payload is missing {key}.")
    returned_id = meta.get("session_id")
    if returned_id and str(returned_id) != session_id:
        raise RuntimeError(f"Source returned a different session_id: {returned_id}")
    return last_turn


def _backup_existing(target: Path) -> Path | None:
    if not target.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(target.name + f".backup-{stamp}")
    shutil.move(str(target), str(backup))
    return backup


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

    turns_path = target / "turns.jsonl"
    fd, tmp_name = tempfile.mkstemp(prefix="turns.", suffix=".tmp", dir=str(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in turns:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp_name, turns_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

    recent = turns[-6:]
    _atomic_json(target / "handoff_tail.json", recent)

    # Never carry transient locks/tokens/packets between hosts.
    for transient in ("turn_packet.json", "resume_token.json", "pending_operation.json"):
        path = target / transient
        if path.exists():
            path.unlink()


def migrate(source: str, session_id: str, data_dir: Path) -> None:
    source = source.rstrip("/")
    session_url = f"{source}/sessions/{session_id}"
    print(f"Reading session metadata from {session_url}")
    session = _get_json(session_url)
    if not isinstance(session, dict):
        raise RuntimeError(f"Unexpected session payload: {type(session).__name__}")

    meta = session.get("meta") if isinstance(session.get("meta"), dict) else {}
    last_turn = int(meta.get("turn_number", 0) or 0)
    turns = _fetch_turns(source, session_id, last_turn) if last_turn else []
    _validate_bundle(session_id, session, turns)

    sessions_dir = data_dir / "sessions"
    target = sessions_dir / session_id
    sessions_dir.mkdir(parents=True, exist_ok=True)
    backup = _backup_existing(target)
    if backup:
        print(f"Existing target session moved to backup: {backup}")

    try:
        _write_session(target, session, turns)
    except Exception:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backup and backup.exists():
            shutil.move(str(backup), str(target))
        raise

    print()
    print("Migration complete.")
    print(f"Session: {session_id}")
    print(f"Turns: {last_turn}")
    print(f"Target: {target}")
    print(f"Verify: /sessions/{session_id}/preview and /sessions/{session_id}/resume")


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy one RomanAI session from a running source backend into local /data.")
    parser.add_argument("session_id", help="RomanAI session id")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"Source backend URL (default: {DEFAULT_SOURCE})")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"Destination DATA_DIR (default: {DEFAULT_DATA_DIR})")
    args = parser.parse_args()

    try:
        migrate(args.source, args.session_id, Path(args.data_dir))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
