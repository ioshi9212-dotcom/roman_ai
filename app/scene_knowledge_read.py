from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .scene_compaction_runtime import complete_knowledge_records
from .transactional_storage import session_transaction


KNOWLEDGE_CHUNK_CHARS = 10000
_STATE_FILE = "scene_knowledge_reads.json"


def _root(session_id: str):
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return root


def _packet(root) -> Dict[str, Any]:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet.get("packet_id"):
        raise RuntimeError("TURN_PACKET_REQUIRED")
    return packet


def _is_v5(source: Dict[str, Any]) -> bool:
    try:
        if int(source.get("version", 1) or 1) >= 5:
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(source.get("profile_schema"), dict)


def _knowledge_payload(session_id: str, character_id: str) -> Dict[str, Any]:
    root = _root(session_id)
    packet = _packet(root)
    source = storage._read_json(root / "source.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = storage._memory_bucket(memory, character_id)

    if _is_v5(source):
        rows = bucket.get("knowledge_journal", [])
        rows = deepcopy(rows) if isinstance(rows, list) else []
        source_kind = "knowledge_journal"
    else:
        rows = complete_knowledge_records(bucket.get("knowledge", []))
        source_kind = "legacy_knowledge"

    return {
        "packet_id": str(packet["packet_id"]),
        "character_id": str(character_id),
        "source_kind": source_kind,
        "entry_count": len(rows),
        "knowledge": rows,
        "complete": True,
        "instruction": (
            "Полный factual knowledge этого персонажа для текущего хода. "
            "Прочитай все chunks этого read_id до написания сцены; не заменяй ранние записи кратким окном."
        ),
    }


def _snapshot(session_id: str, character_id: str) -> tuple[str, List[str], Dict[str, Any]]:
    payload = _knowledge_payload(session_id, character_id)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    read_id = digest[:24]
    chunks = [text[i:i + KNOWLEDGE_CHUNK_CHARS] for i in range(0, len(text), KNOWLEDGE_CHUNK_CHARS)] or ["{}"]
    return read_id, chunks, payload


def _read_state(root) -> Dict[str, Any]:
    value = storage._read_json(root / _STATE_FILE, {})
    return value if isinstance(value, dict) else {}


def _write_progress(root, *, packet_id: str, character_id: str, read_id: str, chunk_count: int, read_chunks: List[int], entry_count: int) -> None:
    state = _read_state(root)
    if str(state.get("packet_id") or "") != packet_id:
        state = {"packet_id": packet_id, "characters": {}}
    characters = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    characters[str(character_id)] = {
        "read_id": read_id,
        "chunk_count": int(chunk_count),
        "read_chunks": sorted(set(int(i) for i in read_chunks if 0 <= int(i) < int(chunk_count))),
        "entry_count": int(entry_count),
    }
    state["characters"] = characters
    storage._write_json(root / _STATE_FILE, state)


def prepare_character_knowledge_read(session_id: str, character_id: str) -> Dict[str, Any]:
    root = _root(session_id)
    read_id, chunks, payload = _snapshot(session_id, character_id)
    packet_id = str(payload["packet_id"])

    with session_transaction(root):
        state = _read_state(root)
        characters = state.get("characters") if (
            str(state.get("packet_id") or "") == packet_id and isinstance(state.get("characters"), dict)
        ) else {}
        row = characters.get(str(character_id)) if isinstance(characters.get(str(character_id)), dict) else {}
        previous = row.get("read_chunks") if (
            str(row.get("read_id") or "") == read_id and isinstance(row.get("read_chunks"), list)
        ) else []
        _write_progress(
            root,
            packet_id=packet_id,
            character_id=character_id,
            read_id=read_id,
            chunk_count=len(chunks),
            read_chunks=[*previous, 0] if chunks else previous,
            entry_count=int(payload["entry_count"]),
        )

    return {
        "session_id": session_id,
        "packet_id": packet_id,
        "character_id": character_id,
        "read_id": read_id,
        "entry_count": int(payload["entry_count"]),
        "chunk_count": len(chunks),
        "chunk_chars_max": KNOWLEDGE_CHUNK_CHARS,
        "first_chunk_included": True,
        "chunk_index": 0,
        "content": chunks[0],
        "all_chunks_read": len(chunks) == 1,
        "next_chunk_index": None if len(chunks) == 1 else 1,
        "instruction": "Chunk 0 уже включён. Дочитай каждый remaining knowledge chunk до сцены.",
    }


def get_character_knowledge_chunk(
    session_id: str,
    character_id: str,
    read_id: str,
    chunk_index: int,
) -> Dict[str, Any]:
    root = _root(session_id)
    current_read_id, chunks, payload = _snapshot(session_id, character_id)
    if current_read_id != read_id:
        raise PermissionError("STALE_CHARACTER_KNOWLEDGE_READ")
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError(chunk_index)

    packet_id = str(payload["packet_id"])
    with session_transaction(root):
        state = _read_state(root)
        characters = state.get("characters") if isinstance(state.get("characters"), dict) else {}
        row = characters.get(str(character_id)) if isinstance(characters.get(str(character_id)), dict) else {}
        already = row.get("read_chunks") if (
            str(row.get("read_id") or "") == read_id and isinstance(row.get("read_chunks"), list)
        ) else []
        read_chunks = sorted(set([*already, int(chunk_index)]))
        _write_progress(
            root,
            packet_id=packet_id,
            character_id=character_id,
            read_id=read_id,
            chunk_count=len(chunks),
            read_chunks=read_chunks,
            entry_count=int(payload["entry_count"]),
        )

    unread = [index for index in range(len(chunks)) if index not in set(read_chunks)]
    return {
        "session_id": session_id,
        "packet_id": packet_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_index": int(chunk_index),
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": not unread,
        "next_chunk_index": unread[0] if unread else None,
    }


def scene_knowledge_read_status(
    session_id: str,
    extra_character_ids: List[str] | None = None,
) -> Dict[str, Any]:
    root = _root(session_id)
    packet = _packet(root)
    packet_id = str(packet["packet_id"])
    state = storage._read_json(root / "state.json", {})
    required = [str(value) for value in storage._scene_participant_ids(state) if value]
    for value in extra_character_ids or []:
        if value and str(value) not in required:
            required.append(str(value))

    progress = _read_state(root)
    rows = progress.get("characters") if (
        str(progress.get("packet_id") or "") == packet_id and isinstance(progress.get("characters"), dict)
    ) else {}

    details: Dict[str, Any] = {}
    incomplete: List[str] = []
    for character_id in required:
        row = rows.get(character_id) if isinstance(rows.get(character_id), dict) else {}
        count = int(row.get("chunk_count", 0) or 0)
        read = {
            int(value)
            for value in row.get("read_chunks", [])
            if isinstance(value, int) and 0 <= int(value) < count
        }
        complete = count > 0 and len(read) == count
        if not complete:
            incomplete.append(character_id)
        details[character_id] = {
            "complete": complete,
            "chunk_count": count,
            "read_chunk_count": len(read),
            "entry_count": int(row.get("entry_count", 0) or 0),
        }

    return {
        "packet_id": packet_id,
        "required_character_ids": required,
        "incomplete_character_ids": incomplete,
        "all_complete": not incomplete,
        "characters": details,
        "instruction": (
            "Для каждого required_character_id вызови prepareCharacterKnowledgeRead, затем все getCharacterKnowledgeChunk. "
            "Сцену нельзя коммитить, пока all_complete=false."
        ),
    }


def require_complete_scene_knowledge_reads(
    session_id: str,
    extra_character_ids: List[str] | None = None,
) -> None:
    status = scene_knowledge_read_status(session_id, extra_character_ids=extra_character_ids)
    if status["incomplete_character_ids"]:
        raise RuntimeError("CHARACTER_KNOWLEDGE_READ_REQUIRED")
