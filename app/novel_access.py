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


def _working_draft_receipt_path(draft_id: str) -> Path:
    return _reads_dir() / f"draft_working_{draft_id}.receipt.json"


def completed_working_draft_revision(draft_id: str) -> int | None:
    payload = storage._read_json(_working_draft_receipt_path(draft_id), {})
    try:
        return int(payload.get("source_revision"))
    except (TypeError, ValueError):
        return None


def _section_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def verify_template(template: Dict[str, Any]) -> Dict[str, Any]:
    characters = template.get("characters", [])
    sections = {}
    for key, value in template.items():
        if key in {"novel_id", "title", "version"}:
            continue
        sections[key] = {
            "present": value is not None,
            "non_empty": bool(value),
            "chars": _section_size(value),
        }
    required_ok = bool(template.get("novel")) and isinstance(characters, list) and len(characters) > 0 and template.get("lore") is not None
    return {
        "ok": required_ok,
        "novel_id": template.get("novel_id"),
        "title": template.get("title"),
        "version": template.get("version", 1),
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "sections": sections,
        "top_level_sections": sorted(template.keys()),
        "total_chars": _section_size(template),
    }


def verify_novel(novel_id: str) -> Dict[str, Any]:
    return verify_template(storage.get_novel(novel_id))


def prepare_template_read(
    template: Dict[str, Any],
    source_type: str,
    source_id: str,
    source_revision: int | None = None,
) -> Dict[str, Any]:
    text = json.dumps(template, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + NOVEL_READ_CHUNK_CHARS] for i in range(0, len(text), NOVEL_READ_CHUNK_CHARS)] or ["{}"]
    read_id = secrets.token_urlsafe(12)
    payload = {
        "read_id": read_id,
        "source_type": source_type,
        "source_id": source_id,
        "source_revision": source_revision,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "read_chunks": [],
    }
    storage._write_json(_read_path(read_id), payload)
    return {
        "read_id": read_id,
        "source_type": source_type,
        "source_id": source_id,
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "instruction": "Read chunks 0 through chunk_count-1 in order.",
    }


def prepare_novel_read(novel_id: str) -> Dict[str, Any]:
    return prepare_template_read(storage.get_novel(novel_id), "library", novel_id)


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
        "source_type": payload.get("source_type"),
        "source_id": payload.get("source_id"),
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": all_read,
    }
    if all_read:
        if payload.get("source_type") == "draft_working" and payload.get("source_revision") is not None:
            receipt_path = _working_draft_receipt_path(str(payload.get("source_id") or ""))
            previous = storage._read_json(receipt_path, {})
            try:
                previous_revision = int(previous.get("source_revision", -1))
            except (TypeError, ValueError):
                previous_revision = -1
            current_revision = int(payload["source_revision"])
            if current_revision >= previous_revision:
                storage._write_json(receipt_path, {
                    "source_id": payload.get("source_id"),
                    "source_revision": current_revision,
                    "read_id": read_id,
                })
        path.unlink(missing_ok=True)
    return result
