import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from . import storage
from .novel_access import prepare_template_read, verify_template


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
    if value[end:].strip():
        raise ValueError("EXTRA_DATA")
    return parsed


def _build_template(draft: Dict[str, Any]) -> Dict[str, Any]:
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
    return template


def _character_ref(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("character_id") or value.get("id") or value.get("name") or value.get("full_name")
    if isinstance(value, (str, int, float)):
        return value
    return None


def _normalise_starting_state_for_session(template: Dict[str, Any]) -> Dict[str, Any]:
    """Make permissive GPT-authored starting_state safe for the strict runtime state shape.

    The finalized canon itself is not changed on disk. Only the isolated session snapshot
    receives this normalized runtime representation.
    """
    result = deepcopy(template)
    raw = result.get("starting_state")
    state = deepcopy(raw) if isinstance(raw, dict) else {}

    current = state.get("current")
    if not isinstance(current, dict):
        current = {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    elif present is None:
        present = []
    elif not isinstance(present, list):
        present = [present]
    current["present_characters"] = present
    state["current"] = current

    pov_raw = state.get("pov")
    if isinstance(pov_raw, dict):
        pov = deepcopy(pov_raw)
    else:
        ref = _character_ref(pov_raw)
        pov = {"character_id": str(ref)} if ref is not None else {}
    state["pov"] = pov

    characters = state.get("characters")
    if not isinstance(characters, dict):
        state["characters"] = {}
    relationships = state.get("relationships")
    if not isinstance(relationships, dict):
        state["relationships"] = {}
    threads = state.get("threads")
    if not isinstance(threads, (dict, list)):
        state["threads"] = {}
    world = state.get("world")
    if not isinstance(world, dict):
        state["world"] = {}

    result["starting_state"] = state
    return result


def create_draft(novel_id: str, title: str, version: int = 1) -> Dict[str, Any]:
    draft_id = uuid.uuid4().hex
    draft = {
        "draft_id": draft_id,
        "novel_id": novel_id,
        "title": title,
        "version": version,
        "sections": {},
        "finalized": False,
        "published_to_library": False,
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
    draft["sections"][section_name] = parsed
    draft["finalized"] = False
    draft.pop("finalized_template", None)
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
        "finalized": bool(draft.get("finalized")),
        "published_to_library": bool(draft.get("published_to_library")),
    }


def finalize_draft(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    status = draft_status(draft_id)
    if not status["ready_to_finalize"]:
        raise ValueError("DRAFT_INCOMPLETE")
    template = _build_template(draft)
    verification = verify_template(template)
    if not verification["ok"]:
        raise RuntimeError("FINAL_VERIFICATION_FAILED")
    draft["finalized"] = True
    draft["finalized_template"] = template
    _write(_draft_path(draft_id), draft)
    return {
        "ok": True,
        "draft_id": draft_id,
        "verification": verification,
        "saved_to_library": False,
        "instruction": "Draft is verified but NOT added to the library. Read it with prepareDraftRead, then createSessionFromDraft. Publish only on explicit user request.",
    }


def _finalized_template(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    template = draft.get("finalized_template")
    if not draft.get("finalized") or not isinstance(template, dict):
        raise RuntimeError("DRAFT_NOT_FINALIZED")
    return template


def prepare_draft_read(draft_id: str) -> Dict[str, Any]:
    return prepare_template_read(_finalized_template(draft_id), "draft", draft_id)


def create_session_from_draft(draft_id: str) -> Dict[str, Any]:
    template = _normalise_starting_state_for_session(_finalized_template(draft_id))
    meta = storage.create_session(template)
    meta["source_type"] = "session_draft"
    return meta


def publish_draft_to_library(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    template = _finalized_template(draft_id)
    storage.save_novel(template)
    draft["published_to_library"] = True
    _write(_draft_path(draft_id), draft)
    return {
        "ok": True,
        "novel_id": template["novel_id"],
        "title": template["title"],
        "published_to_library": True,
    }
