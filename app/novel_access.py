import json
import secrets
from pathlib import Path
from typing import Any, Dict

from . import storage


NOVEL_READ_CHUNK_CHARS = 6000


def _reads_dir() -> Path:
    path = storage.DATA_DIR / "novel_reads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_path(read_id: str) -> Path:
    return _reads_dir() / f"{read_id}.json"


def _working_draft_receipt_path(draft_id: str) -> Path:
    return _reads_dir() / f"draft_working_{draft_id}.receipt.json"


def _working_draft_active_path(draft_id: str) -> Path:
    return _reads_dir() / f"draft_working_{draft_id}.active.json"


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

    # A long working-draft reconciliation can span more than one assistant tool
    # window. Re-preparing the same revision must resume the existing read,
    # never silently restart it from chunk 0.
    if source_type == "draft_working" and source_revision is not None:
        active_path = _working_draft_active_path(str(source_id))
        active = storage._read_json(active_path, {})
        active_id = str(active.get("read_id") or "")
        try:
            active_revision = int(active.get("source_revision"))
        except (TypeError, ValueError):
            active_revision = None
        if active_id and active_revision == int(source_revision) and _read_path(active_id).exists():
            payload = storage._read_json(_read_path(active_id), {})
            if (
                payload.get("source_type") == source_type
                and str(payload.get("source_id") or "") == str(source_id)
                and int(payload.get("source_revision", -1)) == int(source_revision)
            ):
                read_chunks = {int(value) for value in payload.get("read_chunks", []) if isinstance(value, int)}
                next_index = next((i for i in range(len(payload.get("chunks", []))) if i not in read_chunks), None)
                return {
                    "read_id": active_id,
                    "source_type": source_type,
                    "source_id": source_id,
                    "source_revision": source_revision,
                    "chunk_count": len(payload.get("chunks", [])),
                    "total_chars": sum(len(chunk) for chunk in payload.get("chunks", [])),
                    "read_chunk_count": len(read_chunks),
                    "next_chunk_index": next_index,
                    "resumed_read": True,
                    "instruction": "Continue this existing read from next_chunk_index. Do not prepare a new read for the same revision.",
                }

        # New revision supersedes any unfinished read of the older revision.
        if active_id and _read_path(active_id).exists():
            _read_path(active_id).unlink(missing_ok=True)
        active_path.unlink(missing_ok=True)

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
    if source_type == "draft_working" and source_revision is not None:
        storage._write_json(_working_draft_active_path(str(source_id)), {
            "read_id": read_id,
            "source_revision": int(source_revision),
        })
    return {
        "read_id": read_id,
        "source_type": source_type,
        "source_id": source_id,
        "source_revision": source_revision,
        "chunk_count": len(chunks),
        "total_chars": len(text),
        "read_chunk_count": 0,
        "next_chunk_index": 0,
        "resumed_read": False,
        "instruction": "Read chunks from next_chunk_index through chunk_count-1 in order. Reuse this read_id until all chunks are read.",
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
    next_index = next((i for i in range(len(chunks)) if i not in read_chunks), None)
    result = {
        "read_id": read_id,
        "source_type": payload.get("source_type"),
        "source_id": payload.get("source_id"),
        "source_revision": payload.get("source_revision"),
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": all_read,
        "next_chunk_index": next_index,
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
            active_path = _working_draft_active_path(str(payload.get("source_id") or ""))
            active = storage._read_json(active_path, {})
            if str(active.get("read_id") or "") == read_id:
                active_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    return result
