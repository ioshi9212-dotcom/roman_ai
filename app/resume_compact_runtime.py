from __future__ import annotations

from typing import Any, Dict

from . import storage


_HEAVY_RESUME_FIELDS = ("chronology_context", "relationships", "relationship_documents")


def _size(value: Any) -> int:
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 0


def _pending_turn(root) -> Dict[str, Any] | None:
    packet = storage._read_json(root / "turn_packet.json", {})
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
    }


def _last_committed_turn(root) -> Dict[str, Any] | None:
    turns = storage._read_turns(root)
    if turns:
        last = turns[-1]
        if isinstance(last, dict):
            return {
                "turn_number": int(last.get("turn_number", 0) or 0),
                "scene_output": str(last.get("scene_output") or ""),
                "handoff_from_previous_session": False,
            }

    handoff = storage._read_json(root / "handoff_tail.json", [])
    bridge_turns = handoff if isinstance(handoff, list) else handoff.get("turns") if isinstance(handoff, dict) else None
    if isinstance(bridge_turns, list) and bridge_turns:
        last = bridge_turns[-1]
        if isinstance(last, dict) and str(last.get("scene_output") or ""):
            return {
                "turn_number": 0,
                "source_turn_number": int(last.get("turn_number", 0) or 0),
                "scene_output": str(last.get("scene_output") or ""),
                "handoff_from_previous_session": True,
            }

    durable = storage._read_json(root / "continuation_handoff.json", {})
    last = durable.get("last_turn") if isinstance(durable, dict) else None
    if isinstance(last, dict) and str(last.get("scene_output") or ""):
        return {
            "turn_number": 0,
            "source_turn_number": int(last.get("turn_number", 0) or 0),
            "scene_output": str(last.get("scene_output") or ""),
            "handoff_from_previous_session": True,
            "handoff_source": "durable_backup",
        }
    return None
