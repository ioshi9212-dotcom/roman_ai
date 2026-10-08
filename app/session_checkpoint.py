"""Lightweight read-only status for sessions with many turns or stale pending packets.

Do not run migrations, replay the entire history, or build writer context to
answer a resume. No canonical writes occur here.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any, Dict

from . import runtime_access, session_migrations, storage
from .operation_receipts import turn_identity


CHECKPOINT_VERSION = 1


def _last_turn(root) -> Dict[str, Any] | None:
    path = root / "turns.jsonl"
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        last_lines = deque((line for line in fh if line.strip()), maxlen=1)
    if not last_lines:
        return None
    row = json.loads(last_lines[0])
    if not isinstance(row, dict):
        raise ValueError("Last archived turn is not an object")
    return row


def _pending(root) -> Dict[str, Any] | None:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet.get("packet_id"):
        return None
    chunks = packet.get("chunks") if isinstance(packet.get("chunks"), list) else []
    read = sorted({i for i in packet.get("read_chunks", []) if isinstance(i, int) and 0 <= i < len(chunks)})
    unread = [i for i in range(len(chunks)) if i not in set(read)]
    stale = not session_migrations.packet_is_current(packet)
    return {
        "packet_id": str(packet["packet_id"]),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "request_id": str(packet.get("request_id") or "") or None,
        "user_input": str(packet.get("user_input") or ""),
        "chunk_count": len(chunks),
        "read_chunks": read,
        "unread_chunk_indices": unread,
        "ready_for_commit": not unread and not stale,
        "runtime_stale": stale,
        "status": "runtime_stale" if stale else "ready_for_commit" if not unread else "reading",
    }


def should_use_checkpoint(session_id: str) -> bool:
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    return (
        int(meta.get("turn_number", 0) or 0) >= 80
        or (root / "turn_packet.json").is_file()
    )


def resume_checkpoint(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        raise FileNotFoundError(session_id)

    meta = storage._read_json(root / "meta.json", {})
    state = storage._read_json(root / "state.json", {})
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    raw_present = current.get("present_characters", [])
    if isinstance(raw_present, dict):
        raw_present = list(raw_present.keys())
    elif isinstance(raw_present, str):
        raw_present = [raw_present]
    present = [str(cid) for cid in raw_present if cid] if isinstance(raw_present, list) else []

    number = int(meta.get("turn_number", 0) or 0)
    row = _last_turn(root)
    archived_number = int(row.get("turn_number", 0) or 0) if row else 0
    pending = _pending(root)
    result: Dict[str, Any] = {
        "ok": True,
        "session_id": session_id,
        "resume_checkpoint_version": CHECKPOINT_VERSION,
        "resume_payload_compact": True,
        "resume_mode": "read_only_checkpoint",
        "turn_number": number,
        "archived_turn_number": archived_number,
        "current_turn_id": turn_identity(row),
        "source_draft_id": meta.get("source_draft_id"),
        "last_audit_turn": int(meta.get("last_audit_turn", 0) or 0),
        "audit_required": bool(meta.get("audit_required")),
        "data_schema_version": meta.get("data_schema_version"),
        "runtime_revision": runtime_access.runtime_revision(),
        "current": {
            "date": current.get("date") or current.get("game_date") or current.get("calendar_date"),
            "time": current.get("time") or current.get("game_time"),
            "location": current.get("location") or current.get("place") or current.get("area"),
            "scene": current.get("scene") or current.get("scene_name") or current.get("situation"),
            "present_character_ids": present,
        },
        "last_committed_turn": {
            "turn_number": archived_number,
            "scene_output": str(row.get("scene_output") or ""),
            "handoff_from_previous_session": False,
        } if row else None,
    }
    if number != archived_number and archived_number > 0:
        result["turn_archive_mismatch"] = {
            "meta_turn_number": number,
            "last_archived_turn_number": archived_number,
        }
    if pending:
        result["pending_turn"] = pending
        if pending["runtime_stale"]:
            result["instruction"] = (
                "The old packet_id is stale after a deployment/rollback. Do NOT commit that "
                "packet_id again. Call prepareTurn for pending_turn.user_input using the "
                "same request_id if known. This rebuilds only uncommitted context; "
                "the archived scene and canon are unchanged."
            )
        else:
            result["instruction"] = (
                "Use this exact pending packet only if all chunks were read, then commit "
                "once; never invent a second turn on a transport error."
            )
    elif meta.get("audit_required"):
        result["instruction"] = (
            "An audit is required. Get an audit snapshot before any new gameplay turn; "
            "do not generate a replacement scene."
        )
    else:
        result["instruction"] = (
            "No pending packet. The last_committed_turn is the exact saved scene. "
            "For the same unfinished user action call prepareTurn with the original input, "
            "then read its chunks and commit exactly once."
        )
    return result
