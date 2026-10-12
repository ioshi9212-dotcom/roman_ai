"""Persistent, canon-neutral image references for a novel.

This module never edits session files, character cards, or scene turns.
Only a separately authorized management API should call its write methods.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import storage
from .transactional_storage import session_transaction


MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 40_000_000
IMAGE_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}
ID_PATTERN = re.compile(r"^[\w-]{1,100}$", re.UNICODE)
REF_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STORED_PATTERN = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")


class VisualLibraryError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _identifier(value: str, what: str) -> str:
    value = str(value or "").strip()
    if not ID_PATTERN.fullmatch(value):
        raise VisualLibraryError(f"INVALID_{what.upper()}")
    return value


def _root(novel_id: str) -> Path:
    return storage.DATA_DIR / "visual_library" / _identifier(novel_id, "novel_id")


def _source_candidates(novel_id: str) -> list[dict[str, Any]]:
    """Resolve published novels AND session-only drafts without mutating either."""
    sources: list[dict[str, Any]] = []
    library_path = storage.LIBRARY_DIR / f"{novel_id}.json"
    if library_path.is_file():
        source = storage._read_json(library_path, {})
        if isinstance(source, dict) and source.get("novel_id") == novel_id:
            sources.append(source)

    if storage.SESSIONS_DIR.is_dir():
        for meta_path in storage.SESSIONS_DIR.glob("*/meta.json"):
            meta = storage._read_json(meta_path, {})
            if not isinstance(meta, dict) or meta.get("source_novel_id") != novel_id:
                continue
            source = storage._read_json(meta_path.parent / "source.json", {})
            if isinstance(source, dict) and source.get("novel_id") == novel_id:
                sources.append(source)

    drafts_dir = storage.DATA_DIR / "novel_drafts"
    if drafts_dir.is_dir():
        for path in drafts_dir.glob("*.json"):
            draft = storage._read_json(path, {})
            if not isinstance(draft, dict) or draft.get("novel_id") != novel_id:
                continue
            finalized = draft.get("finalized_template")
            if isinstance(finalized, dict):
                sources.append(finalized)
            sections = draft.get("sections")
            if isinstance(sections, dict):
                sources.append({"novel_id": novel_id, **sections})
    return sources


def _validate_subject(novel_id: str, subject_type: str, subject_id: str) -> tuple[str, str]:
    kind = str(subject_type or "").strip()
    if kind not in {"novel", "character", "location"}:
        raise VisualLibraryError("INVALID_SUBJECT_TYPE")
    if kind == "novel":
        subject_id = novel_id
    else:
        subject_id = _identifier(subject_id, "subject_id")
    sources = _source_candidates(novel_id)
    if not sources:
        raise VisualLibraryError("NOVEL_NOT_FOUND", 404)
    if kind == "novel":
        return kind, subject_id
    for source in sources:
        if kind == "character":
            cards = source.get("characters", [])
            if isinstance(cards, list) and any(
                isinstance(card, dict) and storage._card_id(card) == subject_id
                for card in cards
            ):
                return kind, subject_id
        if kind == "location":
            locations = source.get("locations", [])
            if isinstance(locations, list) and any(
                isinstance(place, dict) and str(place.get("location_id") or "") == subject_id
                for place in locations
            ):
                return kind, subject_id
    raise VisualLibraryError("SUBJECT_NOT_FOUND", 404)


def _manifest(root: Path, novel_id: str) -> dict[str, Any]:
    data = storage._read_json(root / "manifest.json", None)
    if data is None:
        return {"schema_version": 1, "novel_id": novel_id, "references": []}
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("novel_id") != novel_id
        or not isinstance(data.get("references"), list)
    ):
        raise VisualLibraryError("VISUAL_MANIFEST_INVALID", 500)
    return data


def _write_manifest(root: Path, data: dict[str, Any]) -> None:
    """Fsync + rename; all writers also hold the per-novel directory lock."""
    temp = root / f".manifest.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, root / "manifest.json")
    finally:
        temp.unlink(missing_ok=True)


def _image_info(raw: bytes) -> tuple[str, str, int, int]:
    if not raw or len(raw) > MAX_BYTES:
        raise VisualLibraryError("IMAGE_SIZE_INVALID", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt = str(image.format or "")
                width, height = image.size
                if fmt not in IMAGE_FORMATS or width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    raise VisualLibraryError("IMAGE_FORMAT_OR_DIMENSIONS_INVALID", 415)
                image.verify()
        media_type, ext = IMAGE_FORMATS[fmt]
        return media_type, ext, width, height
    except VisualLibraryError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise VisualLibraryError("INVALID_IMAGE_CONTENT", 415) from exc


def _save_asset(root: Path, data: bytes, ext: str) -> str:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    file_name = f"{uuid.uuid4().hex}{ext}"
    temp = root / "assets" / f".{uuid.uuid4().hex}.upload"
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, root / "assets" / file_name)
    finally:
        temp.unlink(missing_ok=True)
    return file_name


def _ref(data: dict[str, Any], ref_id: str) -> dict[str, Any]:
    for item in data["references"]:
        if isinstance(item, dict) and item.get("reference_id") == ref_id:
            return item
    raise VisualLibraryError("REFERENCE_NOT_FOUND", 404)


def _asset_path(root: Path, stored_name: str) -> Path:
    if not STORED_PATTERN.fullmatch(str(stored_name or "")):
        raise VisualLibraryError("INVALID_ASSET_NAME", 500)
    return root / "assets" / stored_name


def list_references(novel_id: str, *, subject_type: str | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
    root = _root(novel_id)
    data = _manifest(root, novel_id)
    entries = data["references"]
    if subject_type:
        entries = [item for item in entries if item.get("subject_type") == subject_type]
    if subject_id:
        entries = [item for item in entries if item.get("subject_id") == subject_id]
    return [{key: value for key, value in item.items() if key != "stored_name"} for item in entries]


def save_reference(novel_id: str, raw: bytes, *, subject_type: str, subject_id: str,
                   category: str = "other", notes: str = "", original_name: str = "",
                   reference_id: str | None = None) -> dict[str, Any]:
    novel_id = _identifier(novel_id, "novel_id")
    subject_type, subject_id = _validate_subject(novel_id, subject_type, subject_id)
    category = str(category or "other").strip()
    notes = str(notes or "").strip()
    if not category or len(category) > 64 or len(notes) > 500:
        raise VisualLibraryError("REFERENCE_METADATA_INVALID")
    media_type, ext, width, height = _image_info(raw)
    digest = hashlib.sha256(raw).hexdigest()
    root = _root(novel_id)
    root.mkdir(parents=True, exist_ok=True)
    with session_transaction(root):
        manifest = _manifest(root, novel_id)
        previous = None
        if reference_id is not None:
            if not REF_PATTERN.fullmatch(reference_id):
                raise VisualLibraryError("INVALID_REFERENCE_ID")
            previous = _ref(manifest, reference_id)
            if previous["subject_type"] != subject_type or previous["subject_id"] != subject_id:
                raise VisualLibraryError("REFERENCE_SUBJECT_MISMATCH", 409)
        item_id = reference_id or uuid.uuid4().hex
        stored_name = _save_asset(root, raw, ext)
        now = datetime.now(timezone.utc).isoformat()
        new_entry = {
            "reference_id": item_id,
            "novel_id": novel_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "category": category,
            "notes": notes,
            "original_name": str(original_name or "").replace("\\", "/").split("/")[-1][:150],
            "stored_name": stored_name,
            "media_type": media_type,
            "size_bytes": len(raw),
            "width": width,
            "height": height,
            "sha256": digest,
            "created_at": previous["created_at"] if previous else now,
            "updated_at": now,
        }
        try:
            if previous:
                manifest["references"] = [
                    new_entry if item.get("reference_id") == item_id else item
                    for item in manifest["references"]
                ]
            else:
                manifest["references"].append(new_entry)
            _write_manifest(root, manifest)
        except Exception:
            _asset_path(root, stored_name).unlink(missing_ok=True)
            raise
        if previous:
            _asset_path(root, previous["stored_name"]).unlink(missing_ok=True)
        return {key: value for key, value in new_entry.items() if key != "stored_name"}


def get_reference_asset(novel_id: str, reference_id: str) -> tuple[Path, str]:
    root = _root(novel_id)
    if not REF_PATTERN.fullmatch(str(reference_id or "")):
        raise VisualLibraryError("INVALID_REFERENCE_ID")
    item = _ref(_manifest(root, novel_id), reference_id)
    path = _asset_path(root, item["stored_name"])
    if not path.is_file():
        raise VisualLibraryError("REFERENCE_FILE_MISSING", 404)
    return path, item["media_type"]


def delete_reference(novel_id: str, reference_id: str) -> dict[str, Any]:
    root = _root(novel_id)
    if not REF_PATTERN.fullmatch(str(reference_id or "")):
        raise VisualLibraryError("INVALID_REFERENCE_ID")
    if not root.is_dir():
        raise VisualLibraryError("REFERENCE_NOT_FOUND", 404)
    with session_transaction(root):
        manifest = _manifest(root, novel_id)
        old = _ref(manifest, reference_id)
        manifest["references"] = [
            item for item in manifest["references"] if item.get("reference_id") != reference_id
        ]
        _write_manifest(root, manifest)
        _asset_path(root, old["stored_name"]).unlink(missing_ok=True)
    return {"deleted": True, "reference_id": reference_id}


def require_visual_key(key: str | None) -> None:
    expected = os.getenv("VISUAL_LIBRARY_API_KEY", "").strip()
    if not expected:
        raise VisualLibraryError("VISUAL_LIBRARY_NOT_CONFIGURED", 503)
    if not key or not secrets.compare_digest(key, expected):
        raise VisualLibraryError("VISUAL_LIBRARY_UNAUTHORIZED", 401)
