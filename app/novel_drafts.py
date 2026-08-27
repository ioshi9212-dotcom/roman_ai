import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from . import storage
from .novel_access import verify_novel


REQUIRED_SECTIONS = ("novel", "characters", "lore")
ALLOWED_SECTIONS = {
    "novel",
    "characters",
    "lore",
    "rules",
    "hidden_lore",
    "world",
    "starting_state",
    "story_direction",
}


def _drafts_dir() -> Path:
    path = storage.DATA_DIR / "novel_drafts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _draft_path(draft_id: str) -> Path:
    return _drafts_dir() / f"{draft_id}.json"


def _write(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read(draft_id: str) -> Dict[str, Any]:
    path = _draft_path(draft_id)
    if not path.exists():
        raise FileNotFoundError(draft_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_one_json(text: str) -> Any:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    parsed, end = decoder.raw_decode(value)
    trailing = value[end:].strip()
    if trailing:
        raise ValueError("EXTRA_DATA")
    return parsed


def create_draft(novel_id: str, title: str, version: int = 1) -> Dict[str, Any]:
    draft_id = uuid.uuid4().hex
    draft = {
        "draft_id": draft_id,
        "novel_id": novel_id,
        "title": title,
        "version": version,
        "sections": {},
    }
    _write(_draft_path(draft_id), draft)
    return draft_status(draft_id)


def save_section(draft_id: str, section_name: str, section_json: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    section_name = section_name.strip()
    if section_name not in ALLOWED_SECTIONS:
        raise KeyError(section_name)
    parsed = _parse_one_json(section_json)
    if section_name == "characters" and not isinstance(parsed, list):
        raise TypeError("characters must be a JSON array")
    if section_name != "characters" and not isinstance(parsed, (dict, list, str, int, float, bool, type(None))):
        raise TypeError("invalid JSON section")
    draft["sections"][section_name] = parsed
    _write(_draft_path(draft_id), draft)
    return draft_status(draft_id)


def draft_status(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    sections = draft.get("sections", {})
    characters = sections.get("characters", [])
    missing = [name for name in REQUIRED_SECTIONS if name not in sections]
    return {
        "draft_id": draft_id,
        "novel_id": draft["novel_id"],
        "title": draft["title"],
        "version": draft.get("version", 1),
        "saved_sections": sorted(sections.keys()),
        "missing_required_sections": missing,
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "ready_to_finalize": not missing and isinstance(characters, list) and len(characters) > 0,
    }


def finalize_draft(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    status = draft_status(draft_id)
    if not status["ready_to_finalize"]:
        raise ValueError("DRAFT_INCOMPLETE")

    sections = deepcopy(draft["sections"])
    template = {
        "novel_id": draft["novel_id"],
        "title": draft["title"],
        "version": draft.get("version", 1),
        "novel": sections.pop("novel"),
        "characters": sections.pop("characters"),
        "lore": sections.pop("lore"),
    }
    template.update(sections)
    storage.save_novel(template)

    verification = verify_novel(template["novel_id"])
    if not verification["ok"]:
        raise RuntimeError("FINAL_VERIFICATION_FAILED")

    path = _draft_path(draft_id)
    if path.exists():
        path.unlink()
    return {
        "ok": True,
        "novel_id": template["novel_id"],
        "verification": verification,
        "instruction": "Server-side verification passed. Do not call getNovel before createSession. Use prepareNovelRead only when full content must actually be inspected.",
    }
