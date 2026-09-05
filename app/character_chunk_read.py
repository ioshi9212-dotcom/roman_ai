from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .character_access import get_character_bundle


CHARACTER_CHUNK_CHARS = 6000


def _snapshot(session_id: str, character_id: str) -> tuple[str, List[str]]:
    bundle = get_character_bundle(session_id, character_id)
    text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    read_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    chunks = [text[i : i + CHARACTER_CHUNK_CHARS] for i in range(0, len(text), CHARACTER_CHUNK_CHARS)] or ["{}"]
    return read_id, chunks


def prepare_character_bundle_read(session_id: str, character_id: str) -> Dict[str, Any]:
    read_id, chunks = _snapshot(session_id, character_id)
    return {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_count": len(chunks),
        "chunk_chars_max": CHARACTER_CHUNK_CHARS,
        "instruction": "Read every character bundle chunk from index 0 through chunk_count-1 before writing this offscreen character into the scene. Use single chunks only.",
    }


def get_character_bundle_chunk(
    session_id: str,
    character_id: str,
    read_id: str,
    chunk_index: int,
) -> Dict[str, Any]:
    current_read_id, chunks = _snapshot(session_id, character_id)
    if current_read_id != read_id:
        raise PermissionError("STALE_CHARACTER_READ")
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError(chunk_index)
    return {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "next_chunk_index": None if chunk_index + 1 >= len(chunks) else chunk_index + 1,
    }
