from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .scene_compaction_runtime import load_scene_history


SCENE_ARCHIVE_CHUNK_CHARS = 12000


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
                "Complete persistent compacted-scene index. If one old scene matters, call prepareSceneArchiveRead again with its scene_id "
                "to get that scene plus the immutable raw turns from its source_ranges."
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
            "This is persistent scene evidence, not character knowledge by itself. Use the scene summary for navigation and the raw_turns for exact historical continuity. "
            "Do not invent details missing from both."
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
            "Without scene_id this returns the complete compacted-scene index; with scene_id it returns exact stored scene evidence including raw turns."
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
