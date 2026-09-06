from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import storage
from .transactional_storage import session_transaction


SNAPSHOT_FILE = "last_turn_snapshot.json"
SNAPSHOT_VERSION = 1
_ORIGINAL_COMMIT_TURN = None


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


def _commit_turn_with_snapshot(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    # Serialize the preflight and snapshot with the real transactional commit.
    # Nested session_transaction calls are intentionally re-entrant.
    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        if meta.get("audit_required"):
            raise RuntimeError("AUDIT_REQUIRED")
        if meta.get("handoff_required"):
            raise RuntimeError("HANDOFF_REQUIRED")

        committed_turn = int(meta.get("turn_number", 0)) + 1
        packet = storage._read_json(root / "turn_packet.json", {})
        if (
            not packet
            or int(packet.get("prepared_for_turn", 0) or 0) != committed_turn
            or packet.get("user_input") != payload.get("user_input")
        ):
            raise RuntimeError("TURN_PACKET_REQUIRED")
        if len(set(packet.get("read_chunks", []))) < int(packet.get("chunk_count", 0) or 0):
            raise RuntimeError("TURN_PACKET_INCOMPLETE")

        # The snapshot is written only after the exact commit preconditions pass.
        # If the following commit fails, meta.turn_number does not advance, so this
        # snapshot cannot be mistaken for a committed turn during rollback.
        storage._write_json(root / SNAPSHOT_FILE, build_pre_turn_snapshot(root, committed_turn))
        result = dict(_ORIGINAL_COMMIT_TURN(session_id, payload))
        result["rollback_snapshot_saved"] = True
        return result


def install() -> None:
    global _ORIGINAL_COMMIT_TURN
    if _ORIGINAL_COMMIT_TURN is not None:
        return
    _ORIGINAL_COMMIT_TURN = storage.commit_turn
    storage.commit_turn = _commit_turn_with_snapshot
