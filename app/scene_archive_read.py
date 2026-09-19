from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .scene_compaction_runtime import load_scene_history
from .transactional_storage import session_transaction


SCENE_ARCHIVE_CHUNK_CHARS = 12000
SCENE_HISTORY_PACKET_CHARS = 12000
MAX_WORKING_SCENES = 12
MAX_RECENT_SCENES = 8
MAX_SCENES_PER_CHARACTER = 2
MAX_SCENES_FOR_LOCATION = 2
SCENE_HISTORY_TRANSPORT_VERSION = 1


def _session_root(session_id: str):
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return root


def _ranges(row: Dict[str, Any]) -> List[List[int]]:
    values = row.get("source_ranges")
    result: List[List[int]] = []
    if isinstance(values, list):
        for pair in values:
            if isinstance(pair, list) and len(pair) == 2:
                try:
                    start, end = int(pair[0]), int(pair[1])
                except (TypeError, ValueError):
                    continue
                if start > 0 and end >= start:
                    result.append([start, end])
    if not result:
        try:
            start = int(row.get("start_turn") or 0)
            end = int(row.get("end_turn") or 0)
        except (TypeError, ValueError):
            start, end = 0, 0
        if start > 0 and end >= start:
            result.append([start, end])
    return result


def _turn_number(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("turn_number") or 0)
    except (TypeError, ValueError):
        return 0


def _scene_payload(session_id: str, scene_id: str | None) -> Dict[str, Any]:
    root = _session_root(session_id)
    scenes = load_scene_history(root)

    if not scene_id:
        return {
            "mode": "scene_index",
            "session_id": session_id,
            "persistent_scene_count": len(scenes),
            "scenes": deepcopy(scenes),
            "raw_turns_included": False,
            "instruction": (
                "Complete persistent compacted-scene index. If an old scene matters, call prepareSceneArchiveRead again "
                "with scene_id to read that stored scene plus its exact raw turns."
            ),
        }

    wanted = str(scene_id)
    scene = next((deepcopy(row) for row in scenes if str(row.get("scene_id") or "") == wanted), None)
    if scene is None:
        raise KeyError(wanted)

    ranges = _ranges(scene)
    turns = storage._read_turns(root)
    selected = [
        deepcopy(turn)
        for turn in turns
        if any(start <= _turn_number(turn) <= end for start, end in ranges)
    ]
    expected_turns = {
        turn
        for start, end in ranges
        for turn in range(start, end + 1)
    }
    actual_turns = {_turn_number(turn) for turn in selected if _turn_number(turn) > 0}
    return {
        "mode": "scene_evidence",
        "session_id": session_id,
        "scene_id": wanted,
        "scene": scene,
        "source_ranges": ranges,
        "raw_turns": selected,
        "raw_turn_evidence_complete": actual_turns == expected_turns,
        "missing_raw_turn_numbers": sorted(expected_turns - actual_turns),
        "instruction": (
            "Persistent historical evidence only. Use the compacted scene for navigation and raw_turns for exact continuity. "
            "This does not grant any character knowledge by itself."
        ),
    }


def _snapshot(session_id: str, scene_id: str | None) -> tuple[str, List[str]]:
    payload = _scene_payload(session_id, scene_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    read_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    chunks = [
        text[index:index + SCENE_ARCHIVE_CHUNK_CHARS]
        for index in range(0, len(text), SCENE_ARCHIVE_CHUNK_CHARS)
    ] or ["{}"]
    return read_id, chunks


def prepare_scene_archive_read(session_id: str, scene_id: str | None = None) -> Dict[str, Any]:
    read_id, chunks = _snapshot(session_id, scene_id)
    result: Dict[str, Any] = {
        "session_id": session_id,
        "scene_id": scene_id,
        "read_id": read_id,
        "chunk_count": len(chunks),
        "chunk_chars_max": SCENE_ARCHIVE_CHUNK_CHARS,
        "first_chunk_included": bool(chunks),
        "next_chunk_index": 1 if len(chunks) > 1 else None,
        "instruction": (
            "Chunk 0 is included. Read only remaining getSceneArchiveChunk indices. "
            "Omit scene_id for the full compacted-scene index; provide scene_id for exact raw scene evidence."
        ),
    }
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(chunks) == 1
    return result


def get_scene_archive_chunk(
    session_id: str,
    scene_id: str | None,
    read_id: str,
    chunk_index: int,
) -> Dict[str, Any]:
    current_read_id, chunks = _snapshot(session_id, scene_id)
    if current_read_id != read_id:
        raise PermissionError("STALE_SCENE_ARCHIVE_READ")
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError(chunk_index)
    return {
        "session_id": session_id,
        "scene_id": scene_id,
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "next_chunk_index": None if chunk_index + 1 >= len(chunks) else chunk_index + 1,
    }


def _scene_end_turn(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("end_turn") or 0)
    except (TypeError, ValueError):
        return 0


def _scene_participants(row: Dict[str, Any]) -> set[str]:
    values = row.get("participants")
    if isinstance(values, str):
        values = [values]
    return {str(value) for value in values if value} if isinstance(values, list) else set()


def _scene_locations(row: Dict[str, Any]) -> set[str]:
    values = row.get("locations")
    if not isinstance(values, list):
        value = row.get("location")
        values = [value] if value else []
    return {str(value).casefold().strip() for value in values if str(value).strip()}


def _context_character_ids(context: Dict[str, Any]) -> List[str]:
    values = [str(value) for value in context.get("relevant_character_ids", []) if value]
    for row in context.get("character_cards", []) if isinstance(context.get("character_cards"), list) else []:
        if isinstance(row, dict) and row.get("character_id"):
            values.append(str(row["character_id"]))
    return list(dict.fromkeys(values))


def _working_scene_history(
    scenes: List[Dict[str, Any]],
    character_ids: List[str],
    location: Any,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    scenes = [deepcopy(row) for row in scenes if isinstance(row, dict)]
    if not scenes:
        return [], {}

    selected: Dict[str, Dict[str, Any]] = {}

    def keep(row: Dict[str, Any]) -> None:
        scene_id = str(row.get("scene_id") or "")
        if scene_id:
            selected[scene_id] = row

    for row in scenes[-MAX_RECENT_SCENES:]:
        keep(row)

    older = scenes[:-MAX_RECENT_SCENES] if len(scenes) > MAX_RECENT_SCENES else []
    for character_id in character_ids:
        matches = [row for row in older if character_id in _scene_participants(row)]
        for row in matches[-MAX_SCENES_PER_CHARACTER:]:
            keep(row)

    needle = str(location or "").casefold().strip()
    if needle:
        matches = [row for row in older if needle in _scene_locations(row)]
        for row in matches[-MAX_SCENES_FOR_LOCATION:]:
            keep(row)

    for row in reversed(scenes):
        if str(row.get("status") or "").casefold() == "open":
            keep(row)
            break

    recent_ids = {
        str(row.get("scene_id") or "")
        for row in scenes[-MAX_RECENT_SCENES:]
        if row.get("scene_id")
    }
    if len(selected) > MAX_WORKING_SCENES:
        recent = [row for row in selected.values() if str(row.get("scene_id") or "") in recent_ids]
        extras = [row for row in selected.values() if str(row.get("scene_id") or "") not in recent_ids]
        extras.sort(key=_scene_end_turn, reverse=True)
        selected = {
            str(row.get("scene_id")): row
            for row in [*recent, *extras[: max(0, MAX_WORKING_SCENES - len(recent))]]
            if row.get("scene_id")
        }

    working = sorted(selected.values(), key=lambda row: (_scene_end_turn(row), str(row.get("scene_id") or "")))
    omitted = max(0, len(scenes) - len(working))
    return working, {
        "persistent_scene_count": len(scenes),
        "working_scene_count": len(working),
        "omitted_scene_count": omitted,
        "complete_archive_persistent": True,
        "working_cap": MAX_WORKING_SCENES,
        "archive_action": "prepareSceneArchiveRead",
    }


def apply_bounded_scene_history(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    root = _session_root(session_id)
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return dict(manifest)
        if packet.get("scene_history_transport_version") == SCENE_HISTORY_TRANSPORT_VERSION:
            return dict(manifest)

        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        context = json.loads(raw)
        scenes = load_scene_history(root)
        current = context.get("scene_state", {}).get("current", {}) if isinstance(context.get("scene_state"), dict) else {}
        location = (current.get("location") or current.get("place")) if isinstance(current, dict) else None
        working, window = _working_scene_history(scenes, _context_character_ids(context), location)
        context["scene_history"] = working
        if window:
            context["scene_history_window"] = window
        contract = context.get("working_context_contract") if isinstance(context.get("working_context_contract"), dict) else {}
        contract["scene_history_transport"] = "bounded_with_lossless_archive"
        contract["scene_history_working_cap"] = MAX_WORKING_SCENES
        context["working_context_contract"] = contract

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [
            text[index:index + SCENE_HISTORY_PACKET_CHARS]
            for index in range(0, len(text), SCENE_HISTORY_PACKET_CHARS)
        ] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["scene_history_transport_version"] = SCENE_HISTORY_TRANSPORT_VERSION
        packet["scene_archive_capable"] = True
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(manifest)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "chunk_chars_max": SCENE_HISTORY_PACKET_CHARS,
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "next_chunk_index": 1 if len(chunks) > 1 else None,
            "all_chunks_read": len(chunks) == 1,
            "scene_history_bounded": True,
            "scene_archive_capable": True,
            "instruction": (
                "Chunk 0 is included. Read remaining turn packet chunks, then commit once. "
                "If an omitted old scene matters, use prepareSceneArchiveRead; persistent history was not deleted."
            ),
        })
        return result
