from __future__ import annotations

import json
import secrets
from typing import Any, Dict

from . import storage
from .runtime_access import runtime_documents


AUDIT_PACKET_VERSION = 2
AUDIT_PACKET_FILE = "audit_packet.json"


def _audit_range(meta: Dict[str, Any]) -> tuple[int, int]:
    end_turn = int(meta.get("turn_number", 0))
    start_turn = max(int(meta.get("last_audit_turn", 0)) + 1, end_turn - 14)
    return start_turn, end_turn


def _build_full_audit_payload(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")

    start_turn, end_turn = _audit_range(meta)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    audit_turns = storage.get_turn_range(session_id, start_turn, end_turn)

    return {
        "audit_packet_version": AUDIT_PACKET_VERSION,
        "session_id": session_id,
        "audit_range": [start_turn, end_turn],
        "runtime_documents_full": runtime_documents(),
        "source_full": source,
        "character_cards_full": cards,
        "state_full": state,
        "memory_full": memory,
        "chronology_full": chronology,
        "audit_turns_full": audit_turns,
        "instruction": (
            "FULL 15-TURN AUDIT. Nothing in this audit payload is summarized, clipped, sampled or omitted. "
            "Read EVERY audit chunk before commitAudit. audit_turns_full contains the exact saved turns in the audit range; "
            "state_full, memory_full and chronology_full are the complete persistent stores at audit time; source_full and "
            "character_cards_full are complete canon/card references; runtime_documents_full contains the complete current rules. "
            "Repair only genuine inconsistencies or missing durable records caused during this audit range. Do not rewrite correct "
            "history, do not bloat chronology, and never copy objective chronology/source/card knowledge into a character's memory "
            "unless an exact turn proves that character personally saw, heard, read, received or was told it."
        ),
    }


def _packet_path(root) -> Any:
    return root / AUDIT_PACKET_FILE


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    """Prepare or resume a lossless chunked audit read and return only its manifest."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    start_turn, end_turn = _audit_range(meta)

    packet_path = _packet_path(root)
    packet = storage._read_json(packet_path, {})
    if (
        isinstance(packet, dict)
        and packet.get("audit_range") == [start_turn, end_turn]
        and isinstance(packet.get("chunks"), list)
        and packet.get("chunks")
    ):
        chunks = packet["chunks"]
        return {
            "ok": True,
            "audit_id": packet["audit_id"],
            "audit_range": [start_turn, end_turn],
            "chunk_count": len(chunks),
            "total_chars": sum(len(chunk) for chunk in chunks),
            "already_read_chunks": packet.get("read_chunks", []),
            "instruction": "Call getAuditSnapshotChunk for EVERY chunk index from 0 to chunk_count-1 before commitAudit. Audit data is full and untruncated.",
        }

    payload = _build_full_audit_payload(session_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
    packet = {
        "audit_id": secrets.token_urlsafe(12),
        "audit_range": [start_turn, end_turn],
        "chunk_count": len(chunks),
        "read_chunks": [],
        "chunks": chunks,
    }
    storage._write_json(packet_path, packet)
    return {
        "ok": True,
        "audit_id": packet["audit_id"],
        "audit_range": [start_turn, end_turn],
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "already_read_chunks": [],
        "instruction": "Call getAuditSnapshotChunk for EVERY chunk index from 0 to chunk_count-1 before commitAudit. Audit data is full and untruncated.",
    }


def get_audit_snapshot_chunk(session_id: str, audit_id: str, chunk_index: int) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    packet = storage._read_json(_packet_path(root), {})
    if not packet or packet.get("audit_id") != audit_id:
        raise PermissionError("INVALID_AUDIT_PACKET")
    chunks = packet.get("chunks", [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")

    read_chunks = set(packet.get("read_chunks", []))
    read_chunks.add(chunk_index)
    packet["read_chunks"] = sorted(read_chunks)
    storage._write_json(_packet_path(root), packet)
    return {
        "audit_id": audit_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": len(read_chunks) == len(chunks),
    }


def require_complete_audit_read(session_id: str, start_turn: int, end_turn: int) -> None:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(_packet_path(root), {})
    if not packet or packet.get("audit_range") != [int(start_turn), int(end_turn)]:
        raise RuntimeError("AUDIT_PACKET_REQUIRED")
    chunks = packet.get("chunks", [])
    if len(set(packet.get("read_chunks", []))) < len(chunks):
        raise RuntimeError("AUDIT_PACKET_INCOMPLETE")


def clear_audit_packet(session_id: str) -> None:
    path = _packet_path(storage.SESSIONS_DIR / session_id)
    if path.exists():
        path.unlink()
