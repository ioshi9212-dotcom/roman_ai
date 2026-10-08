"""Bounded, read-only resume for long-running sessions.

A resume is an acknowledgement/status request, not a gameplay preparation.
Do not rebuild all character memory and replay the entire turn archive just
to show the latest committed scene and an uncommitted packet.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any, Dict

from . import (
    fast_audit_runtime,
    resume_compact_runtime,
    runtime_access,
    session_migrations,
    session_recovery,
    storage,
)
from .character_registry import build_character_registry
from .operation_receipts import turn_identity


def _last_turn(root) -> Dict[str, Any] | None:
    archive = root / "turns.jsonl"
    if archive.is_file():
        # Scan text lines once, parse only the last record. In particular do
        # not parse all 100+ saved extracted/state payloads into memory.
        with archive.open("r", encoding="utf-8") as file:
            last_lines = deque((line for line in file if line.strip()), maxlen=1)
        if last_lines:
            row = json.loads(last_lines[0])
            if isinstance(row, dict):
                return row
    return None


def is_large_or_pending(session_id: str) -> bool:
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    return int(meta.get("turn_number", 0) or 0) >= 80 or (root / "turn_packet.json").is_file()


def fast_resume(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        raise FileNotFoundError(session_id)

    meta = storage._read_json(root / "meta.json", {})
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    recent = _last_turn(root)
    if recent:
        last_committed = {
            "turn_number": int(recent.get("turn_number", 0) or 0),
            "scene_output": str(recent.get("scene_output") or ""),
            "handoff_from_previous_session": False,
        }
        last_id = turn_identity(recent)
    else:
        # Very first turn of a migrated continuation may use the handoff
        # scene as its durable last-scene bridge.
        last_committed = resume_compact_runtime._last_committed_turn(root)
        last_id = None

    status = session_recovery.current_recovery_status(session_id)
    pending = resume_compact_runtime._pending_turn(root)
    result: Dict[str, Any] = {
        "ok": True,
        "session_id": session_id,
        "source_draft_id": meta.get("source_draft_id"),
        "turn_number": int(meta.get("turn_number", 0) or 0),
        "current_turn_id": last_id,
        "last_audit_turn": int(meta.get("last_audit_turn", 0) or 0),
        "audit_required": bool(meta.get("audit_required")),
        "current": {
            "date": current.get("date") or current.get("game_date") or current.get("calendar_date"),
            "time": current.get("time") or current.get("game_time"),
            "location": current.get("location") or current.get("place") or current.get("area"),
            "scene": current.get("scene") or current.get("scene_name") or current.get("situation"),
            "present_character_ids": status.get("present_character_ids", []),
        },
        "character_registry": build_character_registry(cards, state),
        "data_schema_version": int(meta.get("data_schema_version", 0) or 0),
        "runtime_revision": runtime_access.runtime_revision(),
        "current_recovery_required": bool(status.get("required")),
        "last_committed_turn": last_committed,
        "resume_payload_compact": True,
        "resume_mode": "bounded_read_only",
    }
    if status.get("required"):
        result["current_recovery_reasons"] = status.get("reasons", [])
    if last_committed and result["turn_number"] != last_committed["turn_number"] and last_committed["turn_number"] != 0:
        result["turn_archive_mismatch"] = {
            "meta_turn_number": result["turn_number"],
            "last_archived_turn_number": last_committed["turn_number"],
        }

    if pending:
        packet = storage._read_json(root / "turn_packet.json", {})
        stale = not session_migrations.packet_is_current(packet)
        pending["runtime_stale"] = stale
        result["pending_turn"] = pending
        result["instruction"] = (
            "Pending packet is from an older runtime. Do not reuse its packet_id; "
            "prepareTurn again for the exact same user_input and request_id. "
            "This only rebuilds uncommitted transport, not saved canon."
            if stale else
            "Reuse this uncommitted packet; read unread_chunk_indices and correct the "
            "last validation error before commitTurn. Do not create a new gameplay turn."
        )
    else:
        result["instruction"] = (
            "Continue this exact persistent session. No gameplay turn was created by resume. "
            "On the next gameplay input call prepareTurn."
        )
    if meta.get("audit_required"):
        result["required_audit"] = fast_audit_runtime.get_audit_snapshot(session_id)
        result["instruction"] = "Complete required_audit before the next gameplay turn."
    return result
