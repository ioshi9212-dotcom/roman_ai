from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

PROFILE_ROOT = Path(__file__).resolve().parent.parent / "state"


PROFILE_VERSION = 3

# In-memory compatibility shapes for the existing draft API; no duplicate template files.
_INTERNAL_SHAPES = {'character': {'character_id': None,
               'name': None,
               'surname': None,
               'aliases': [],
               'age': None,
               'status': None,
               'role': None,
               'is_pov': False,
               'story_function': None,
               'appearance': None,
               'character': None,
               'speech': None,
               'habits': None,
               'work': None,
               'residence': None,
               'relationships': None,
               'abilities': None,
               'weaknesses': None,
               'goals': None,
               'background': None,
               'secrets_known_to_self': None,
               'notes': None,
               'generated_details': {},
               'additional': {}},
 'novel': {'title': None,
           'genres': [],
           'category': None,
           'pov_character': None,
           'setting': None,
           'premise': None,
           'tone': None,
           'world_rules': None,
           'supernatural': None,
           'story_rules': None,
           'start': None,
           'core_cast': [],
           'notes': None,
           'additional': {}},
 'location': {'location_id': None,
              'name': None,
              'aliases': [],
              'type': None,
              'parent_location_id': None,
              'where': None,
              'floor': None,
              'hours': None,
              'staff': [],
              'linked_characters': [],
              'layout': None,
              'zones': [],
              'appearance': None,
              'fixed_features': [],
              'notes': None,
              'additional': {}},
 'hidden_lore': {'entries': []},
 'knowledge_entry': {'date': None, 'period': None, 'text': None},
 'canon_note': {'note_id': None, 'text': None, 'subjects': []}}

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

_LOCATION_ALIASES = {
    "id": "location_id",
    "title": "name",
    "kind": "type",
    "parent": "parent_location_id",
    "parent_id": "parent_location_id",
    "address": "where",
    "district": "where",
    "opening_hours": "hours",
    "work_hours": "hours",
    "workers": "staff",
    "employees": "staff",
    "staff_roles": "staff",
    "characters": "linked_characters",
    "people": "linked_characters",
    "rooms": "zones",
    "areas": "zones",
    "style": "appearance",
    "interior": "appearance",
    "features": "fixed_features",
}

_NOVEL_ALIASES = {
    "pov": "pov_character",
    "pov_id": "pov_character",
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

_LOCATION_LABELS = (
    ("name", "Название"),
    ("type", "Тип"),
    ("parent_location_id", "Родительская локация"),
    ("where", "Где находится"),
    ("floor", "Этаж"),
    ("hours", "Часы работы"),
    ("staff", "Работники"),
    ("linked_characters", "Связанные персонажи"),
    ("layout", "Планировка"),
    ("zones", "Постоянные зоны"),
    ("appearance", "Общий вид"),
    ("fixed_features", "Фиксированные особенности"),
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


def load_profile_template(name: str) -> Dict[str, Any]:
    if name not in _INTERNAL_SHAPES:
        raise KeyError(name)
    return deepcopy(_INTERNAL_SHAPES[name])


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
        # Template notes and internal metadata describe storage, not canon.
        if str(raw_key).startswith("_"):
            continue
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
    from .session_documents import card_to_internal
    source = card_to_internal(raw) if isinstance(raw, dict) else {}
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

    raw_pov = profile.get("is_pov")
    profile["is_pov"] = raw_pov is True or str(raw_pov or "").casefold().strip() in {"true", "1", "yes", "да"}
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


_LEGACY_POV_SILENCE_RE = re.compile(
    r"(?iu)(?:^|(?<=[.!?;]))\s*если\s+игрок\s+не\s+дал\s+реплику\s*[,—:-]?\s*не\s+придумыва(?:ть|й)\s+(?:её|ее)\s*[.!?;]*"
)


def _strip_legacy_pov_silence_rule(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = _LEGACY_POV_SILENCE_RE.sub(" ", value)
        return " ".join(cleaned.split()).strip(" ;")
    if isinstance(value, list):
        result = []
        for item in value:
            cleaned = _strip_legacy_pov_silence_rule(item)
            if cleaned not in (None, "", [], {}):
                result.append(cleaned)
        return result
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _strip_legacy_pov_silence_rule(item)) not in (None, "", [], {})
        }
    return deepcopy(value)


def normalize_novel_profile(raw: Any, *, title: str | None = None) -> Dict[str, Any]:
    from .session_documents import novel_to_internal
    source = novel_to_internal(raw) if isinstance(raw, dict) and "название" in raw else deepcopy(raw) if isinstance(raw, dict) else {}
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

    # Author questionnaire is canon. Do not rewrite its prose during intake.
    # Any retired runtime command is removed only from the *writer view*
    # by turn_pipeline._clean_director_layers, never from stored source.
    # This preserves punctuation, explicit harsh wording and original spacing.

    core_cast = profile.get("core_cast")
    if not isinstance(core_cast, list):
        core_cast = []
    profile["core_cast"] = deepcopy(core_cast)
    return profile


def normalize_location_profile(raw: Any) -> Dict[str, Any]:
    from .session_documents import location_to_internal
    if isinstance(raw, dict):
        raw = location_to_internal(raw)
    source = deepcopy(raw) if isinstance(raw, dict) else {}
    profile = _merge_known_fields(
        source,
        base=load_profile_template("location"),
        aliases=_LOCATION_ALIASES,
    )

    location_id = (
        profile.get("location_id")
        or source.get("id")
        or source.get("name")
        or source.get("title")
    )
    if location_id not in (None, ""):
        profile["location_id"] = str(location_id).strip()

    aliases = profile.get("aliases")
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list):
        aliases = []
    profile["aliases"] = [str(value).strip() for value in aliases if str(value).strip()]

    staff = profile.get("staff")
    if isinstance(staff, str):
        staff = [staff]
    elif isinstance(staff, dict):
        staff = [
            f"{str(key).strip()}: {str(value).strip()}"
            if value not in (None, "", [], {})
            else str(key).strip()
            for key, value in staff.items()
            if str(key).strip()
        ]
    if not isinstance(staff, list):
        staff = []
    profile["staff"] = [str(value).strip() for value in staff if str(value).strip()]

    linked = profile.get("linked_characters")
    if isinstance(linked, str):
        linked = [linked]
    elif isinstance(linked, dict):
        if any(key in linked for key in ("character_id", "id", "name")):
            linked = [linked]
        else:
            expanded_linked = []
            for key, value in linked.items():
                if not str(key).strip():
                    continue
                row = {"character_id": str(key)}
                if isinstance(value, dict):
                    relation = value.get("relation") or value.get("role") or value.get("connection")
                else:
                    relation = value
                if relation not in (None, "", [], {}):
                    row["relation"] = relation
                expanded_linked.append(row)
            linked = expanded_linked
    if not isinstance(linked, list):
        linked = []
    normalized_linked: List[Dict[str, Any]] = []
    for item in linked:
        if isinstance(item, str):
            cid = item.strip()
            if cid:
                normalized_linked.append({"character_id": cid})
            continue
        if not isinstance(item, dict):
            continue
        cid = str(item.get("character_id") or item.get("id") or item.get("name") or "").strip()
        if not cid:
            continue
        row = {"character_id": cid}
        relation = item.get("relation") or item.get("role") or item.get("connection")
        if relation not in (None, ""):
            row["relation"] = str(relation).strip()
        normalized_linked.append(row)
    profile["linked_characters"] = normalized_linked

    zones = profile.get("zones")
    if isinstance(zones, str):
        zones = [zones]
    elif isinstance(zones, dict):
        expanded_zones = []
        for key, value in zones.items():
            if isinstance(value, dict):
                row = deepcopy(value)
                row.setdefault("zone_id", str(key))
                row.setdefault("name", str(key))
            else:
                row = {
                    "zone_id": str(key),
                    "name": str(key),
                    "summary": value,
                }
            expanded_zones.append(row)
        zones = expanded_zones
    if not isinstance(zones, list):
        zones = []
    normalized_zones: List[Dict[str, Any]] = []
    for index, item in enumerate(zones):
        if isinstance(item, str):
            name = item.strip()
            row = {"zone_id": _norm_key(name) or f"zone_{index + 1}", "name": name}
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or item.get("zone_id") or item.get("id") or "").strip()
            zid = str(item.get("zone_id") or item.get("id") or _norm_key(name) or f"zone_{index + 1}").strip()
            row = {"zone_id": zid, "name": name or zid}
            summary = item.get("summary") or item.get("description") or item.get("note")
            if summary not in (None, ""):
                row["summary"] = str(summary).strip()
        else:
            continue
        normalized_zones.append(row)
    profile["zones"] = normalized_zones

    fixed = profile.get("fixed_features")
    if isinstance(fixed, str):
        fixed = [fixed]
    if not isinstance(fixed, list):
        fixed = []
    profile["fixed_features"] = [str(value).strip() for value in fixed if str(value).strip()]
    return profile


def normalize_location_profiles(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        expanded = []
        for key, value in raw.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, dict):
                row = deepcopy(value)
                row.setdefault("location_id", str(key))
            else:
                row = {"location_id": str(key), "name": str(value)}
            expanded.append(row)
        raw = expanded
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        profile = normalize_location_profile(item)
        location_id = str(profile.get("location_id") or "").strip()
        if not location_id:
            continue
        # Preserve duplicates here. Setup validation reports duplicate canonical
        # ids explicitly instead of silently dropping source material.
        result.append(profile)
    return result


def normalize_canon_notes(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, str):
        raw = [{"text": raw}]
    elif isinstance(raw, dict):
        if isinstance(raw.get("notes"), list):
            raw = raw["notes"]
        else:
            raw = [
                {"note_id": str(key), "text": value}
                if not isinstance(value, dict)
                else {"note_id": str(key), **deepcopy(value)}
                for key, value in raw.items()
                if not str(key).startswith("_")
            ]
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        note = load_profile_template("canon_note")
        note_id = str(item.get("note_id") or item.get("id") or f"note_{index + 1}").strip()
        text = str(item.get("text") or item.get("note") or item.get("fact") or item.get("summary") or "").strip()
        if not note_id or not text or note_id in seen:
            continue
        subjects = item.get("subjects")
        if subjects is None:
            subjects = item.get("subject_ids") or item.get("links") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        if not isinstance(subjects, list):
            subjects = []
        note["note_id"] = note_id
        note["text"] = text
        note["subjects"] = [str(value).strip() for value in subjects if str(value).strip()]
        seen.add(note_id)
        result.append(note)
    return result


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
                if not str(key).startswith("_") and value not in (None, "", [], {})
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


def render_location_profile(raw: Dict[str, Any]) -> str:
    profile = normalize_location_profile(raw)
    lines: List[str] = []
    for key, label in _LOCATION_LABELS:
        value = _render_value(profile.get(key))
        if value:
            lines.append(f"{label}: {value}")
    # Keep unknown/raw extras in persistent storage for losslessness, but do not
    # inject them into every scene. Location context is intentionally a short
    # structural profile, not a second lore dump.
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
    from .session_documents import FILES, template, public
    return {
        "state_document_format": "generator",
        "state_template_count": len(FILES),
        "state_files": list(FILES),
        "state_fields": {name: list(public(template(name))) for name in FILES},
        "state_files_are_persistence_layout": True,
        "default_draft_version": 5,
        "save_requires_expected_revision": True,
        "setup_sections": {
            "novel": {"type": "object", "template": "анкета.json", "pov_field": "pov_id / pov_character"},
            "characters": {"type": "array", "items_template": "карточка_персонажа.json", "identity_fields": ["id", "имя"]},
            "locations": {"type": "array", "items_template": "локации.json._формат_локации"},
            "hidden_lore": {"type": "object", "shape": {"entries": ["director-only text"]}},
            "knowledge": {"type": "object", "shape": {"character_id": ["personally known starting fact"]}},
            "canon_notes": {"type": "array", "item_fields": ["note_id", "text", "subjects"]},
            "lore": {"type": "object"},
        },
        "character_relationship_example": {
            "target_character_id": "pov_id",
            "relationship_context": "Only the relationship given in RAW",
            "dimensions": [{"label": "author-selected axis", "value": 25}],
        },
        "version": PROFILE_VERSION,
        "novel_fields": list(load_profile_template("novel").keys()),
        "character_fields": list(load_profile_template("character").keys()),
        "hidden_lore_fields": list(load_profile_template("hidden_lore").keys()),
        "knowledge_entry_fields": list(load_profile_template("knowledge_entry").keys()),
        "location_fields": list(load_profile_template("location").keys()),
        "canon_note_fields": list(load_profile_template("canon_note").keys()),
        "rule": (
            "Save only named API sections through saveNovelDraftSection with the latest expected_revision. "
            "The ten state files describe persisted session layout, not ten required draft sections. "
            "Do not send template notes or format examples as canon. "
            "Missing optional fields may stay empty. V5 needs novel, characters and reviewed RAW intake; "
            "starting_state is supplied through setDraftLaunchState after finalize. "
            "NPC-to-NPC relationships use text without dimensions. No foundation/source-unit ledger or startup quote audit is required."
        ),
    }
