from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import storage
from .main import app
from .turn_rollback import rollback_last_turn


TARGET_SESSION = "5d1651de1d37470280ef2e576862ff63"
EXPECTED_TURN = 167
TARGET_TURN = 166


def _assert_json_file(root: Path, name: str):
    path = root / name
    if not path.exists():
        raise RuntimeError(f"ROLLBACK_VERIFY_MISSING:{name}")
    return storage._read_json(path, None)


def _run_once() -> None:
    root = storage.SESSIONS_DIR / TARGET_SESSION
    if not root.exists():
        raise RuntimeError("ROLLBACK_TARGET_SESSION_NOT_FOUND")

    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0)
    if current_turn == TARGET_TURN:
        turns = storage._read_turns(root)
        if not turns or int(turns[-1].get("turn_number", 0) or 0) != TARGET_TURN:
            raise RuntimeError("ROLLBACK_ALREADY_166_BUT_TURNS_MISMATCH")
        print(
            "ROLLBACK_ONCE_NOOP "
            + json.dumps(
                {
                    "session_id": TARGET_SESSION,
                    "turn_number": TARGET_TURN,
                    "reason": "already_rolled_back",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    if current_turn != EXPECTED_TURN:
        raise RuntimeError(f"ROLLBACK_UNEXPECTED_CURRENT_TURN:{current_turn}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = storage.DATA_DIR / "rollback_backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{TARGET_SESSION}-before-turn-{EXPECTED_TURN}-{stamp}"
    if backup.exists():
        raise RuntimeError("ROLLBACK_BACKUP_COLLISION")
    shutil.copytree(root, backup)

    before_meta = storage._read_json(backup / "meta.json", {})
    before_turns = storage._read_turns(backup)
    if int(before_meta.get("turn_number", 0) or 0) != EXPECTED_TURN:
        raise RuntimeError("ROLLBACK_BACKUP_META_MISMATCH")
    if not before_turns or int(before_turns[-1].get("turn_number", 0) or 0) != EXPECTED_TURN:
        raise RuntimeError("ROLLBACK_BACKUP_TURNS_MISMATCH")

    result = rollback_last_turn(TARGET_SESSION, EXPECTED_TURN, True)

    after_meta = _assert_json_file(root, "meta.json")
    _assert_json_file(root, "state.json")
    _assert_json_file(root, "characters.json")
    _assert_json_file(root, "memory.json")
    _assert_json_file(root, "chronology.json")
    _assert_json_file(root, "audits.json")
    after_turns = storage._read_turns(root)

    if int(after_meta.get("turn_number", 0) or 0) != TARGET_TURN:
        raise RuntimeError("ROLLBACK_VERIFY_META_NOT_166")
    if not after_turns or int(after_turns[-1].get("turn_number", 0) or 0) != TARGET_TURN:
        raise RuntimeError("ROLLBACK_VERIFY_LAST_TURN_NOT_166")
    if any(int(turn.get("turn_number", 0) or 0) >= EXPECTED_TURN for turn in after_turns if isinstance(turn, dict)):
        raise RuntimeError("ROLLBACK_VERIFY_TURN_167_STILL_PRESENT")

    verification = {
        "ok": True,
        "session_id": TARGET_SESSION,
        "backup": str(backup),
        "result": result,
        "verified_turn_number": TARGET_TURN,
        "last_saved_turn": int(after_turns[-1].get("turn_number", 0) or 0),
        "state_ok": True,
        "characters_ok": True,
        "memory_ok": True,
        "chronology_ok": True,
        "audits_ok": True,
    }
    (backup / "ROLLBACK_RESULT.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("ROLLBACK_ONCE_SUCCESS " + json.dumps(verification, ensure_ascii=False), flush=True)


_run_once()
