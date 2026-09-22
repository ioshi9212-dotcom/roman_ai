from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import storage


SNAPSHOT_FILE = "last_turn_snapshot.json"
PREVIOUS_SNAPSHOT_FILE = "previous_turn_snapshot.json"
PREVIOUS2_SNAPSHOT_FILE = "previous_turn_snapshot_2.json"
SNAPSHOT_VERSION = 2


def build_pre_turn_snapshot(root, committed_turn: int) -> Dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "committed_turn": int(committed_turn),
        "previous_turn": int(committed_turn) - 1,
        "meta": deepcopy(storage._read_json(root / "meta.json", {})),
        "characters": deepcopy(storage._read_json(root / "characters.json", [])),
        "state": deepcopy(storage._read_json(root / "state.json", {})),
        "memory": deepcopy(storage._read_json(root / "memory.json", {})),
        "chronology": deepcopy(storage._read_json(root / "chronology.json", [])),
        "audits": deepcopy(storage._read_json(root / "audits.json", [])),
    }
