from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import storage


PROFILE_VERSION = 1
_TEMPLATE_FILES = {
    "character": "character_profile.json",
    "novel": "novel_profile.json",
    "hidden_lore": "hidden_lore.json",
    "knowledge_entry": "knowledge_journal_entry.json",
}

_CHARACTER_ALIASES = {
    "id": "character_id",
    "full_name": "name",
    "first_name": "name",
    "last_name": "surname",
    "family_name": "surname",
    "type": "status",
    "species": "status",
    "story_role": "role",
    "personality": "character",
    "temperament": "character",
    "voice": "speech",
    "speech_style": "speech",
    "job": "work",
    "occupation": "work",
    "profession": "work",
    "home": "residence",
    "address": "residence",
    "housing": "residence",
    "backstory": "background",
    "past": "background",
    "history": "background",
    "secret": "secrets_known_to_self",
    "secrets": "secrets_known_to_self",
}

_NOVEL_ALIASES = {
    "pov": "pov_character",
    "pov_character_id": "pov_character",
    "main_pov": "pov_character",
    "genres_list": "genres",
    "world": "setting",
    "world_description": "setting",
    "setup": "premise",
    "hook": "premise",
    "rules": "story_rules",
    "start_state": "start",
    "starting_point": "start",
}

_CHARACTER_LABELS = (
    ("name", "Имя"),
    ("surname", "Фамилия"),
    ("age", "Возраст"),
    ("status", "Статус"),
    ("role", "Роль"),
    ("story_function", "Функция в истории"),
    ("appearance", "Внешность"),
    ("character", "Характер"),
    ("speech", "Манера речи"),
    ("habits", "Привычки"),
    ("work", "Работа"),
    ("residence", "Место проживания"),
    ("relationships", "Связи и отношения"),
    ("abilities", "Способности"),
    ("weaknesses", "Слабости"),
    ("goals", "Цели"),
    ("background", "Прошлое"),
    ("secrets_known_to_self", "Секреты, которые знает о себе"),
    ("notes", "Дополнительно"),
)

_NOVEL_LABELS = (
    ("title", "Название"),
    ("genres", "Жанры"),
    ("category", "Категория"),
    ("pov_character", "POV"),
    ("setting", "Сеттинг"),
    ("premise", "Завязка"),
    ("tone", "Тон"),
    ("world_rules", "Правила мира"),
    ("supernatural", "Сверхъестественное"),
    ("story_rules", "Правила истории"),
    ("start", "Старт"),
    ("core_cast", "Основные персонажи"),
    ("notes", "Дополнительно"),
)


def _template_path(name: str) -> Path:
    filename = _TEMPLATE_FILES[name]
    return storage.TEMPLATE_DIR / filename


def load_profile_template(name: str) -> Dict[str, Any]:
    if name not in _TEMPLATE_FILES:
        raise KeyError(name)
    value = storage._read_json(_template_path(name), {})
    if not isinstance(value, dict):
        raise RuntimeError(f"PROFILE_TEMPLATE_INVALID:{name}")
    return deepcopy(value)


def _norm_key(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _merge_known_fields(
    raw: Dict[str, Any],
    *,
    base: Dict[str, Any],
    aliases: Dict[str, str],
) -> Dict[str, Any]:
    result = deepcopy(base)
    additional: Dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = aliases.get(_norm_key(raw_key), str(raw_key))
        if key in result and key != "additional":
            result[key] = deepcopy(raw_value)
        elif key == "additional" and isinstance(raw_value, dict):
            additional.update(deepcopy(raw_value))
        else:
            additional[str(raw_key)] = deepcopy(raw_value)
    existing = result.get("additional")
    if isinstance(existing, dict):
        merged = deepcopy(existing)
        merged.update(additional)
        result["additional"] = merged
    else:
        result["additional"] = additional
    return result


def _split_full_name(profile: Dict[str, Any]) -> None:
    name = profile.get("name")
    surname = profile.get("surname")
    if surname not in (None, "") or not isinstance(name, str):
        return
    parts = [part for part in name.strip().split() if part]
    if len(parts) == 2:
        profile["name"] = parts[0]
        profile["surname"] = parts[1]


def normalize_character_profile(raw: Any) -> Dict[str, Any]:
    source = deepcopy(raw) if isinstance(raw, dict) else {}
    profile = _merge_known_fields(
        source,
        base=load_profile_template("character"),
        aliases=_CHARACTER_ALIASES,
    )
    _split_full_name(profile)

    character_id = (
        profile.get("character_id")
        or source.get("id")
        or source.get("name")
        or source.get("full_name")
    )
    if character_id not in (None, ""):
        profile["character_id"] = str(character_id).strip()

    aliases = profile.get("aliases")
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list):
        aliases = []
    profile["aliases"] = [str(value) for value in aliases if value not in (None, "")]

    generated = profile.get("generated_details")
    profile["generated_details"] = deepcopy(generated) if isinstance(generated, dict) else {}

    profile["is_pov"] = bool(profile.get("is_pov"))
    return profile


def normalize_character_profiles(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        profile = normalize_character_profile(item)
        character_id = str(profile.get("character_id") or "").strip()
        if not character_id or character_id in seen:
            continue
        seen.add(character_id)
        result.append(profile)
    return result


def normalize_novel_profile(raw: Any, *, title: str | None = None) -> Dict[str, Any]:
    source = deepcopy(raw) if isinstance(raw, dict) else {}
    profile = _merge_known_fields(
        source,
        base=load_profile_template("novel"),
        aliases=_NOVEL_ALIASES,
    )
    if profile.get("title") in (None, "") and title:
        profile["title"] = str(title)

    genres = profile.get("genres")
    if isinstance(genres, str):
        genres = [genres]
    if not isinstance(genres, list):
        genres = []
    profile["genres"] = [str(value) for value in genres if value not in (None, "")]

    core_cast = profile.get("core_cast")
    if not isinstance(core_cast, list):
        core_cast = []
    profile["core_cast"] = deepcopy(core_cast)
    return profile


def normalize_hidden_lore(raw: Any) -> Dict[str, Any]:
    template = load_profile_template("hidden_lore")
    if raw in (None, "", [], {}):
        return template

    if isinstance(raw, str):
        entries = [raw]
    elif isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        values = raw.get("entries")
        if isinstance(values, list):
            entries = values
        elif isinstance(values, str):
            entries = [values]
        else:
            entries = [
                f"{key}: {value}"
                for key, value in raw.items()
                if value not in (None, "", [], {})
            ]
    else:
        entries = [str(raw)]

    template["entries"] = [
        str(value).strip()
        for value in entries
        if str(value).strip()
    ]
    return template


def normalize_knowledge_entry(raw: Any) -> Dict[str, Any] | None:
    if isinstance(raw, str):
        text = raw.strip()
        raw = {"text": text}
    if not isinstance(raw, dict):
        return None
    entry = load_profile_template("knowledge_entry")
    for key in ("date", "period", "text"):
        if raw.get(key) not in (None, ""):
            entry[key] = deepcopy(raw[key])
    if entry.get("text") in (None, ""):
        for alias in ("fact", "summary", "event", "description", "content", "note"):
            if raw.get(alias) not in (None, ""):
                entry["text"] = str(raw[alias]).strip()
                break
    if entry.get("text") in (None, ""):
        return None
    return entry


def normalize_knowledge_journal(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: List[Dict[str, Any]] = []
    for raw in values:
        entry = normalize_knowledge_entry(raw)
        if entry is not None:
            result.append(entry)
    return result


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_render_value(item) for item in value if item not in (None, "", [], {})]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            rendered = _render_value(item)
            if rendered:
                parts.append(f"{key}: {rendered}")
        return "; ".join(parts)
    if value is None:
        return ""
    return str(value)


def render_character_profile(raw: Dict[str, Any]) -> str:
    profile = normalize_character_profile(raw)
    lines: List[str] = []
    for key, label in _CHARACTER_LABELS:
        value = _render_value(profile.get(key))
        if value:
            lines.append(f"{label}: {value}")
    if profile.get("is_pov"):
        lines.append("POV: да")
    additional = _render_value(profile.get("additional"))
    if additional:
        lines.append(f"Прочие данные: {additional}")
    generated = _render_value(profile.get("generated_details"))
    if generated:
        lines.append(f"Достроенные бытовые детали: {generated}")
    return "\n".join(lines).strip()


def render_novel_profile(raw: Dict[str, Any]) -> str:
    profile = normalize_novel_profile(raw)
    lines: List[str] = []
    for key, label in _NOVEL_LABELS:
        value = _render_value(profile.get(key))
        if value:
            lines.append(f"{label}: {value}")
    additional = _render_value(profile.get("additional"))
    if additional:
        lines.append(f"Прочие данные: {additional}")
    return "\n".join(lines).strip()


def render_hidden_lore(raw: Any) -> str:
    lore = normalize_hidden_lore(raw)
    entries = lore.get("entries", [])
    if not isinstance(entries, list):
        return ""
    return "\n".join(f"- {str(value).strip()}" for value in entries if str(value).strip())


def render_knowledge_journal(values: Any) -> str:
    entries = normalize_knowledge_journal(values)
    lines: List[str] = []
    previous_heading: tuple[str, str] | None = None
    for entry in entries:
        date = str(entry.get("date") or "").strip()
        period = str(entry.get("period") or "").strip()
        heading = (date, period)
        if heading != previous_heading and (date or period):
            heading_text = " · ".join(part for part in (date, period) if part)
            lines.append(heading_text)
            previous_heading = heading
        lines.append(str(entry.get("text") or "").strip())
    return "\n".join(line for line in lines if line).strip()


def profile_manifest() -> Dict[str, Any]:
    return {
        "version": PROFILE_VERSION,
        "novel_fields": list(load_profile_template("novel").keys()),
        "character_fields": list(load_profile_template("character").keys()),
        "hidden_lore_fields": list(load_profile_template("hidden_lore").keys()),
        "knowledge_entry_fields": list(load_profile_template("knowledge_entry").keys()),
        "rule": "Templates are fixed. Missing fields may stay empty; do not invent new top-level profile shapes.",
    }
