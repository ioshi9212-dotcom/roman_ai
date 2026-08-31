import json
import secrets
from pathlib import Path
from typing import Any, Dict

from . import storage


RESUME_CHUNK_CHARS = 12000


def _reads_dir() -> Path:
    path = storage.DATA_DIR / "resume_reads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_path(session_id: str, read_id: str) -> Path:
    return _reads_dir() / f"{session_id}__{read_id}.json"


def prepare_resume_read(session_id: str) -> Dict[str, Any]:
    package = storage.build_resume_package(session_id)
    text = json.dumps(package, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + RESUME_CHUNK_CHARS] for i in range(0, len(text), RESUME_CHUNK_CHARS)] or ["{}"]
    read_id = secrets.token_urlsafe(12)
    payload = {
        "session_id": session_id,
        "read_id": read_id,
        "resume_token": package["resume_token"],
        "chunk_count": len(chunks),
        "chunks": chunks,
        "read_chunks": [],
    }
    storage._write_json(_read_path(session_id, read_id), payload)
    return {
        "session_id": session_id,
        "read_id": read_id,
        "resume_token": package["resume_token"],
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "instruction": "Read every resume chunk from 0 through chunk_count-1 in order, reconstruct the JSON package, then call confirmResume with resume_token only after all chunks were read.",
    }


def get_resume_chunk(session_id: str, read_id: str, chunk_index: int) -> Dict[str, Any]:
    path = _read_path(session_id, read_id)
    if not path.exists():
        raise FileNotFoundError(read_id)
    payload = storage._read_json(path, {})
    if payload.get("session_id") != session_id or payload.get("read_id") != read_id:
        raise PermissionError("INVALID_RESUME_READ")
    chunks = payload.get("chunks", [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")

    read_chunks = set(payload.get("read_chunks", []))
    read_chunks.add(chunk_index)
    payload["read_chunks"] = sorted(read_chunks)
    storage._write_json(path, payload)
    all_read = len(read_chunks) == len(chunks)

    result = {
        "session_id": session_id,
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": all_read,
    }
    if all_read:
        path.unlink(missing_ok=True)
    return result
