import json
import secrets
from pathlib import Path
from typing import Any, Dict

from . import storage


NOVEL_READ_CHUNK_CHARS = 12000


def _reads_dir() -> Path:
    path = storage.DATA_DIR / "novel_reads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_path(read_id: str) -> Path:
    return _reads_dir() / f"{read_id}.json"


def _section_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def verify_novel(novel_id: str) -> Dict[str, Any]:
    novel = storage.get_novel(novel_id)
    characters = novel.get("characters", [])
    sections = {}
    for key, value in novel.items():
        if key in {"novel_id", "title", "version"}:
            continue
        sections[key] = {
            "present": value is not None,
            "non_empty": bool(value),
            "chars": _section_size(value),
        }

    required_ok = bool(novel.get("novel")) and isinstance(characters, list) and len(characters) > 0 and novel.get("lore") is not None
    return {
        "ok": required_ok,
        "novel_id": novel.get("novel_id"),
        "title": novel.get("title"),
        "version": novel.get("version", 1),
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "sections": sections,
        "top_level_sections": sorted(novel.keys()),
        "total_chars": _section_size(novel),
    }


def prepare_novel_read(novel_id: str) -> Dict[str, Any]:
    novel = storage.get_novel(novel_id)
    text = json.dumps(novel, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + NOVEL_READ_CHUNK_CHARS] for i in range(0, len(text), NOVEL_READ_CHUNK_CHARS)] or ["{}"]
    read_id = secrets.token_urlsafe(12)
    payload = {
        "read_id": read_id,
        "novel_id": novel_id,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "read_chunks": [],
    }
    storage._write_json(_read_path(read_id), payload)
    return {
        "read_id": read_id,
        "novel_id": novel_id,
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "instruction": "Read chunks 0 through chunk_count-1 in order. Do not call getNovel for large novels.",
    }


def get_novel_read_chunk(read_id: str, chunk_index: int) -> Dict[str, Any]:
    path = _read_path(read_id)
    if not path.exists():
        raise FileNotFoundError(read_id)
    payload = storage._read_json(path, {})
    chunks = payload.get("chunks", [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")
    read_chunks = set(payload.get("read_chunks", []))
    read_chunks.add(chunk_index)
    payload["read_chunks"] = sorted(read_chunks)
    storage._write_json(path, payload)
    all_read = len(read_chunks) == len(chunks)
    result = {
        "read_id": read_id,
        "novel_id": payload.get("novel_id"),
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": all_read,
    }
    if all_read:
        path.unlink(missing_ok=True)
    return result
