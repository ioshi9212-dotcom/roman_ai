from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from . import audit_runtime, session_runtime, storage
from .operation_receipts import (
    OperationReceiptConflict,
    current_turn_identity,
    replay_result,
    request_fingerprint,
)
from .scene_archive_read import apply_bounded_scene_history
from .scene_knowledge_read import require_complete_scene_knowledge_reads, scene_knowledge_read_status
from .transactional_storage import session_transaction
from .turn_duplicate_guard import (
    committed_request_turn,
    duplicate_prepare_response,
    recent_duplicate_turn,
)
from .turn_rollback import RollbackError, rollback_last_turn


def _session_root(session_id: str):
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return root


def _packet_status(packet: Any) -> Dict[str, Any] | None:
    if not isinstance(packet, dict) or not packet.get("packet_id"):
        return None
    chunks = packet.get("chunks") if isinstance(packet.get("chunks"), list) else []
    read = sorted({
        int(value)
        for value in packet.get("read_chunks", [])
        if isinstance(value, int) and 0 <= int(value) < len(chunks)
    })
    unread = [index for index in range(len(chunks)) if index not in set(read)]
    return {
        "packet_id": str(packet.get("packet_id")),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "request_id": str(packet.get("request_id") or "") or None,
        "user_input": str(packet.get("user_input") or ""),
        "chunk_count": len(chunks),
        "read_chunks": read,
        "unread_chunk_indices": unread,
        "ready_for_commit": not unread,
        "status": "ready_for_commit" if not unread else "reading",
        "scene_archive_capable": bool(packet.get("scene_archive_capable")),
        "knowledge_review_capable": bool(packet.get("knowledge_review_capable")),
        "complete_knowledge_read_capable": bool(packet.get("complete_knowledge_read_capable")),
        "strict_knowledge_capable": bool(packet.get("strict_knowledge_capable")),
    }


def pending_turn_status(session_id: str) -> Dict[str, Any] | None:
    root = _session_root(session_id)
    return _packet_status(storage._read_json(root / "turn_packet.json", {}))


def _archive_abandoned_pending(root, packet: Dict[str, Any], *, reason: str) -> None:
    status = _packet_status(packet)
    if status is None:
        return
    path = root / "abandoned_turn_packets.json"
    rows = storage._read_json(path, [])
    rows = rows if isinstance(rows, list) else []
    rows.append({
        **status,
        "abandoned_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    })
    storage._write_json(path, rows[-20:])


def prepare_turn_request(
    session_id: str,
    user_input: str,
    request_id: str | None = None,
    *,
    scene_archive_capable: bool = False,
    knowledge_review_capable: bool = False,
    complete_knowledge_read_capable: bool = False,
    strict_knowledge_capable: bool = False,
    replace_pending: bool = False,
) -> Dict[str, Any]:
    root = _session_root(session_id)
    identity = str(request_id or "").strip()

    with session_transaction(root):
        # A completed request replay is independent of any newer pending turn and must
        # never disturb it.
        if identity:
            committed = committed_request_turn(session_id, identity, user_input)
            if committed is not None:
                return duplicate_prepare_response(committed)

        packet = storage._read_json(root / "turn_packet.json", {})
        status = _packet_status(packet)
        if status is not None:
            same_input = str(packet.get("user_input") or "") == str(user_input)
            pending_id = str(packet.get("request_id") or "").strip()

            if same_input:
                if identity and pending_id and identity != pending_id:
                    raise RuntimeError("TURN_IN_PROGRESS")
                if identity and not pending_id:
                    packet["request_id"] = identity
                    storage._write_json(root / "turn_packet.json", packet)
                result = dict(session_runtime.prepare_turn_packet(session_id, user_input))
                if identity:
                    result["request_id"] = identity
                result["scene_archive_capable"] = bool(packet.get("scene_archive_capable"))
                result["knowledge_review_capable"] = bool(packet.get("knowledge_review_capable"))
                result["complete_knowledge_read_capable"] = bool(packet.get("complete_knowledge_read_capable"))
                result["strict_knowledge_capable"] = bool(packet.get("strict_knowledge_capable"))
                if bool(packet.get("complete_knowledge_read_capable")):
                    result["scene_knowledge_reads"] = scene_knowledge_read_status(session_id)
                result["pending_turn"] = pending_turn_status(session_id)
                return result

            if not replace_pending:
                raise RuntimeError("TURN_IN_PROGRESS")

            _archive_abandoned_pending(root, packet, reason="explicit_replace_pending")
            (root / "turn_packet.json").unlink(missing_ok=True)

        if not identity:
            # Backward-compatible path for an old Custom GPT schema. It behaves exactly
            # like the legacy client until request_id support is enabled client-side.
            duplicate = recent_duplicate_turn(session_id, user_input)
            if duplicate is not None:
                return duplicate_prepare_response(duplicate)

        result = dict(session_runtime.prepare_turn_packet(session_id, user_input))
        packet = storage._read_json(root / "turn_packet.json", {})
        if isinstance(packet, dict) and packet.get("packet_id"):
            if identity:
                packet["request_id"] = identity
            packet["scene_archive_capable"] = bool(scene_archive_capable)
            packet["knowledge_review_capable"] = bool(knowledge_review_capable)
            packet["complete_knowledge_read_capable"] = bool(complete_knowledge_read_capable)
            packet["strict_knowledge_capable"] = bool(strict_knowledge_capable)
            storage._write_json(root / "turn_packet.json", packet)

        if scene_archive_capable:
            result = apply_bounded_scene_history(session_id, result)

        if identity:
            result["request_id"] = identity
        result["scene_archive_capable"] = bool(scene_archive_capable)
        result["knowledge_review_capable"] = bool(knowledge_review_capable)
        result["complete_knowledge_read_capable"] = bool(complete_knowledge_read_capable)
        result["strict_knowledge_capable"] = bool(strict_knowledge_capable)
        if bool(complete_knowledge_read_capable):
            result["scene_knowledge_reads"] = scene_knowledge_read_status(session_id)
        result["pending_turn"] = pending_turn_status(session_id)
        return result


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
    if bool(packet.get("complete_knowledge_read_capable")):
        require_complete_scene_knowledge_reads(session_id)

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
    "prepare_turn_request",
    "pending_turn_status",
    "commit_turn_request",
    "commit_audit_request",
    "rollback_last_turn_request",
]
