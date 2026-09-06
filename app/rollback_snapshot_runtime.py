from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import stability_runtime, storage
from .transactional_storage import json_text


SNAPSHOT_FILE = "last_turn_snapshot.json"
SNAPSHOT_VERSION = 2
_ORIGINAL_STABILITY_WRITE_BATCH = None


def _read_optional_json(root, name: str) -> Dict[str, Any]:
    path = root / name
    if not path.exists():
        return {"exists": False, "value": None}
    return {"exists": True, "value": deepcopy(storage._read_json(path, None))}


def build_pre_turn_snapshot(root, committed_turn: int) -> Dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "snapshot_kind": "exact_pre_turn_files",
        "committed_turn": int(committed_turn),
        "previous_turn": int(committed_turn) - 1,
        "meta": deepcopy(storage._read_json(root / "meta.json", {})),
        "characters": deepcopy(storage._read_json(root / "characters.json", [])),
        "state": deepcopy(storage._read_json(root / "state.json", {})),
        "memory": deepcopy(storage._read_json(root / "memory.json", {})),
        "chronology": deepcopy(storage._read_json(root / "chronology.json", [])),
        "audits": deepcopy(storage._read_json(root / "audits.json", [])),
        "auxiliary": {
            "handoff_tail.json": _read_optional_json(root, "handoff_tail.json"),
            "resume_token.json": _read_optional_json(root, "resume_token.json"),
        },
    }


def _is_turn_commit(root, values: Dict[str, str]) -> tuple[bool, int]:
    required = {
        "turns.jsonl",
        "characters.json",
        "state.json",
        "memory.json",
        "chronology.json",
        "meta.json",
    }
    if not required.issubset(values):
        return False, 0
    old_meta = storage._read_json(root / "meta.json", {})
    try:
        import json

        new_meta = json.loads(values["meta.json"])
        old_turn = int(old_meta.get("turn_number", 0) or 0)
        new_turn = int(new_meta.get("turn_number", 0) or 0)
    except (TypeError, ValueError, KeyError):
        return False, 0
    return new_turn == old_turn + 1, new_turn


def _write_batch_with_snapshot(root, values: Dict[str, str]) -> None:
    payload = dict(values)
    is_turn_commit, committed_turn = _is_turn_commit(root, payload)
    if is_turn_commit:
        payload[SNAPSHOT_FILE] = json_text(build_pre_turn_snapshot(root, committed_turn))
    _ORIGINAL_STABILITY_WRITE_BATCH(root, payload)


def install() -> None:
    global _ORIGINAL_STABILITY_WRITE_BATCH
    if _ORIGINAL_STABILITY_WRITE_BATCH is not None:
        return
    _ORIGINAL_STABILITY_WRITE_BATCH = stability_runtime.write_batch
    stability_runtime.write_batch = _write_batch_with_snapshot
