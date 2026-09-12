from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import audit_runtime, session_runtime, storage
from .operation_receipts import (
    OperationReceiptConflict,
    current_turn_identity,
    replay_result,
    request_fingerprint,
)
from .turn_rollback import RollbackError, rollback_last_turn
from .pov_participation_guard import validate_pov_participation


def _session_root(session_id: str):
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return root


def commit_turn_request(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = _session_root(session_id)
    packet_id = str(payload.get("packet_id") or "").strip()
    if not packet_id:
        raise RuntimeError("TURN_PACKET_ID_REQUIRED")
    fingerprint = request_fingerprint("commit_turn", packet_id, payload)
    replay = replay_result(
        root,
        operation="commit_turn",
        identity=packet_id,
        fingerprint=fingerprint,
    )
    if replay is not None:
        replay.setdefault("already_committed", True)
        return replay

    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or str(packet.get("packet_id") or "") != packet_id:
        raise RuntimeError("TURN_PACKET_REQUIRED")

    validate_pov_participation(session_id, str(payload.get("scene_output") or ""))

    prepared = deepcopy(payload)
    prepared["_operation_receipt"] = {
        "operation": "commit_turn",
        "identity": packet_id,
        "request_fingerprint": fingerprint,
    }
    result = dict(session_runtime.commit_turn(session_id, prepared))
    result.setdefault("already_committed", False)
    result.setdefault("idempotent_replay", False)
    return result


def commit_audit_request(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = _session_root(session_id)
    audit_id = str(payload.get("audit_id") or "").strip()
    if not audit_id:
        raise RuntimeError("AUDIT_PACKET_ID_REQUIRED")
    fingerprint = request_fingerprint("commit_audit", audit_id, payload)
    replay = replay_result(
        root,
        operation="commit_audit",
        identity=audit_id,
        fingerprint=fingerprint,
    )
    if replay is not None:
        audit_runtime.clear_audit_packet(session_id)
        return replay

    audit_runtime.require_complete_audit_read(
        session_id,
        int(payload.get("start_turn", 0) or 0),
        int(payload.get("end_turn", 0) or 0),
        audit_id=audit_id,
    )
    prepared = deepcopy(payload)
    prepared["_operation_receipt"] = {
        "operation": "commit_audit",
        "identity": audit_id,
        "request_fingerprint": fingerprint,
    }
    result = dict(session_runtime.commit_audit(session_id, prepared))
    audit_runtime.clear_audit_packet(session_id)
    result.setdefault("idempotent_replay", False)
    return result


def rollback_last_turn_request(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = _session_root(session_id)
    expected_turn = int(payload.get("expected_turn_number", 0) or 0)
    expected_turn_id = str(payload.get("expected_turn_id") or "").strip()
    if not expected_turn_id:
        raise RollbackError("ROLLBACK_TURN_ID_REQUIRED")

    fingerprint = request_fingerprint("rollback_last_turn", expected_turn_id, payload)
    replay = replay_result(
        root,
        operation="rollback_last_turn",
        identity=expected_turn_id,
        fingerprint=fingerprint,
    )
    if replay is not None:
        return replay

    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0)
    if current_turn != expected_turn:
        raise RollbackError("ROLLBACK_EXPECTED_TURN_MISMATCH")
    current_id = current_turn_identity(root)
    if not current_id or current_id != expected_turn_id:
        raise RollbackError("ROLLBACK_EXPECTED_TURN_ID_MISMATCH")

    receipt = {
        "operation": "rollback_last_turn",
        "identity": expected_turn_id,
        "request_fingerprint": fingerprint,
    }
    return rollback_last_turn(
        session_id,
        expected_turn_number=expected_turn,
        confirm=bool(payload.get("confirm")),
        expected_turn_id=expected_turn_id,
        operation_receipt=receipt,
    )


__all__ = [
    "OperationReceiptConflict",
    "commit_turn_request",
    "commit_audit_request",
    "rollback_last_turn_request",
]
