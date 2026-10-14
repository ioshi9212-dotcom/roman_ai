"""Generator document layout, with lossless views for the existing Roman API.

The Russian files are the live data. English names below are in-memory views,
never a second saved memory or chronology. Private record metadata preserves
original IDs/turns for replay, audit and rollback; it is excluded from writer
context. Version 1-4 sessions can keep their existing format.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "state"
FILES = (
    "state.json", "анкета.json", "карточка_персонажа.json", "локации.json",
    "отношения.json", "память_персонажа.json", "персонажи.json", "сессия.json",
    "скрытый_лор_и_крючки.json", "хронология.json",
)
_CACHE: ContextVar[dict | None] = ContextVar("session_document_read_view", default=None)
_VIRTUAL = {"meta.json", "source.json", "characters.json", "state.json", "memory.json", "chronology.json", "relationships.json"}
_RENAMED = {"source.json": ".runtime/source.json", "turns.jsonl": "ходы.jsonl", "audits.json": ".runtime/audits.json"}


@contextmanager
def read_view() -> Iterator[None]:
    if _CACHE.get() is not None:
        yield
        return
    token = _CACHE.set({})
    try:
        yield
    finally:
        _CACHE.reset(token)


def read(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return deepcopy(default)
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cache = _CACHE.get()
    if cache is not None and key in cache:
        return deepcopy(cache[key])
    value = json.loads(path.read_text(encoding="utf-8"))
    if cache is not None:
        cache[key] = value
    return deepcopy(value)


def template(name: str) -> dict:
    if name not in FILES:
        raise KeyError(name)
    return read(TEMPLATE_ROOT / name, {})


def public(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return deepcopy(value)


def is_native(root: Path) -> bool:
    return root != TEMPLATE_ROOT and (root / "сессия.json").is_file()


def file_path(root: Path, name: str) -> Path:
    return root / (_RENAMED.get(name, name) if is_native(root) else name)


def _id(value: Any) -> str:
    cid = str(value or "").strip()
    if not cid or cid in {".", ".."} or any(c in cid for c in ("/", "\\", "\0")):
        raise ValueError("SESSION_DOCUMENT_CHARACTER_ID_INVALID")
    return cid


def _day(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text or "без даты"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _extras(original: dict, mapped: set[str]) -> dict:
    return {k: deepcopy(v) for k, v in original.items() if k not in mapped}


CARD_FIELDS = {
    "character_id": "id", "name": "имя", "surname": "фамилия", "age": "возраст",
    "status": "вид", "character": "характер", "speech": "речь", "habits": "привычки",
    "work": "занятия_и_образ_жизни", "goals": "цели_и_мотивы", "background": "прошлое",
    "abilities": "способности_и_ограничения", "notes": "особые_пометки", "additional": "дополнительно",
    "gender": "пол", "birth_date": "дата_рождения", "height": "рост_см",
    "internal_conflict": "внутренний_конфликт", "likes": "нравится", "dislikes": "не_нравится",
    "attractiveness": "привлекательность", "hidden_lore": "скрытый_лор",
}
NOVEL_FIELDS = {
    "title": "название", "genres": "жанры", "category": "возрастная_категория",
    "setting": "сеттинг", "premise": "завязка", "tone": "тон_и_атмосфера",
    "story_rules": "правила_новеллы", "start": "начальная_ситуация", "notes": "дополнительно",
    "start_year": "год_начала", "start_date": "дата_начала", "lore": "открытый_лор",
    "scene_style": "подача_сцен",
}
LOCATION_FIELDS = {"location_id": "id", "name": "название", "appearance": "общий_вид",
                   "floor": "этажи", "layout": "планировка", "zones": "помещения", "notes": "дополнительно"}


def location_to_native(location: dict) -> dict:
    if "название" in location:
        return deepcopy(location)
    shape = template("локации.json")["_формат_локации"]
    result = {key: [] if isinstance(value, list) else "" for key, value in shape.items()}
    for old, new in LOCATION_FIELDS.items():
        if old in location:
            result[new] = deepcopy(location[old])
    result["_runtime"] = _extras(location, set(LOCATION_FIELDS))
    return result


def location_to_internal(location: dict) -> dict:
    if "название" not in location:
        return deepcopy(location)
    result = deepcopy(location.get("_runtime", {}))
    for old, new in LOCATION_FIELDS.items():
        if new in location:
            result[old] = deepcopy(location[new])
    return result


def card_to_native(card: dict, existing: dict | None = None) -> dict:
    result = deepcopy(existing) if existing else template("карточка_персонажа.json")
    if "имя" in card:
        result.update(deepcopy(card))
        result["id"] = _id(card.get("id") or card.get("character_id"))
        return result
    from .profile_templates import _CHARACTER_ALIASES
    card = deepcopy(card)
    for alias, canonical in _CHARACTER_ALIASES.items():
        if alias in card and card.get(canonical) in (None, ""):
            card[canonical] = deepcopy(card[alias])
    cid = _id(card.get("character_id") or card.get("id") or card.get("name"))
    extra = card.get("additional") if isinstance(card.get("additional"), dict) else {}
    for old, new in CARD_FIELDS.items():
        if old in card:
            result[new] = deepcopy(card[old])
        elif old in extra:
            result[new] = deepcopy(extra[old])
    result["id"] = cid
    aliases = card.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    result["прозвища"] = [{"прозвище": a, "кто_знает": ""} if isinstance(a, str) else deepcopy(a) for a in aliases]
    appearance = card.get("appearance")
    if isinstance(appearance, dict):
        result["внешность"] = deepcopy(appearance)
    elif appearance is not None:
        result["внешность"] = {"общее_описание": appearance}
    result["_runtime"] = {
        "profile": _extras(card, set(CARD_FIELDS) | {"id", "aliases", "appearance"}),
        "appearance_scalar": not isinstance(appearance, dict) and appearance is not None,
    }
    return result


def card_to_internal(card: dict) -> dict:
    if "имя" not in card:
        return deepcopy(card)
    runtime = card.get("_runtime", {})
    result = {k: deepcopy(v) for k, v in card.items()
              if not k.startswith("_") and k not in set(CARD_FIELDS.values()) | {"прозвища", "внешность"}}
    result.update(deepcopy(runtime.get("profile", {})))
    for old, new in CARD_FIELDS.items():
        if new in card:
            result[old] = deepcopy(card[new])
    result["character_id"] = str(card.get("id") or "")
    result["aliases"] = [a.get("прозвище", "") if isinstance(a, dict) else a for a in card.get("прозвища", [])]
    appearance = deepcopy(card.get("внешность", {}))
    result["appearance"] = appearance.get("общее_описание") if runtime.get("appearance_scalar") and isinstance(appearance, dict) else appearance
    return result


def novel_to_native(source: dict) -> dict:
    result = template("анкета.json")
    novel = source.get("novel", {})
    if not isinstance(novel, dict):
        novel = {}
    extra = novel.get("additional") if isinstance(novel.get("additional"), dict) else {}
    for old, new in NOVEL_FIELDS.items():
        if old in novel:
            result[new] = deepcopy(novel[old])
        elif old in extra:
            result[new] = deepcopy(extra[old])
    result["название"] = source.get("title") or novel.get("title") or ""
    result["открытый_лор"] = deepcopy(source.get("lore", novel.get("lore", "")))
    current = source.get("starting_state", {}).get("current", {})
    if current.get("date"):
        result["дата_начала"] = _day(current["date"])
    result["_runtime"] = {"novel": _extras(novel, set(NOVEL_FIELDS))}
    return result


def novel_to_internal(document: dict) -> dict:
    result = {k: deepcopy(v) for k, v in document.items()
              if not k.startswith("_") and k not in set(NOVEL_FIELDS.values())}
    result.update(deepcopy(document.get("_runtime", {}).get("novel", {})))
    for old, new in NOVEL_FIELDS.items():
        if new in document:
            result[old] = deepcopy(document[new])
    return result


def state_to_native(state: dict, existing: dict | None = None) -> dict:
    if "pov_id" in state:
        result = template("state.json")
        result.update(deepcopy(state))
        return result
    result = deepcopy(existing) if existing else template("state.json")
    current, pov = state.get("current", {}), state.get("pov", {})
    pid = str(pov.get("character_id") or "")
    result.update({
        "дата": _day(current.get("date")) if current.get("date") else "",
        "период_суток": current.get("period") or current.get("period_of_day") or "",
        "точное_время": current.get("time") or "",
        "локация": {"id": current.get("location_id"), "название": current.get("location") or ""},
        "что_происходит_сейчас": current.get("scene") or "", "pov_id": pid,
    })
    rows = state.get("characters", {})
    def participant(value: Any, remote: bool = False) -> dict:
        cid = str((value.get("character_id") or value.get("id") or value.get("name")) if isinstance(value, dict) else value)
        info = deepcopy(rows.get(cid, {}))
        if isinstance(value, dict):
            info.update(value)
        if cid == pid:
            info.update(pov)
        row = {"id": cid}
        if remote:
            row["связь"] = current.get("remote_channels", {}).get(cid) or info.get("channel") or ""
        else:
            row.update({"одежда": info.get("clothing", ""), "при_себе": deepcopy(info.get("inventory", [])),
                        "расположение": current.get("positions", {}).get(cid) or info.get("position") or ""})
        return row
    present = current.get("present_characters", [])
    if isinstance(present, str):
        present = [present]
    remote = current.get("remote_characters", [])
    if isinstance(remote, str):
        remote = [remote]
    result["физически"] = [participant(v) for v in present]
    result["удалённо"] = [participant(v, True) for v in remote]
    result["_runtime"] = {
        "state": _extras(state, {"current", "pov", "characters"}),
        "current": _extras(current, {"date", "time", "period", "period_of_day", "location", "location_id", "scene", "present_characters", "remote_characters", "remote_channels", "positions"}),
        "pov": _extras(pov, {"character_id", "clothing", "inventory", "position"}),
        "characters": {cid: _extras(row, {"clothing", "inventory", "position"}) for cid, row in rows.items() if isinstance(row, dict)},
    }
    return result


def state_to_internal(document: dict) -> dict:
    if "pov_id" not in document:
        return deepcopy(document)
    rt = document.get("_runtime", {})
    result = deepcopy(rt.get("state", {}))
    current = deepcopy(rt.get("current", {}))
    location = document.get("локация", {})
    current.update({"date": document.get("дата") or None, "time": document.get("точное_время") or None,
                    "period": document.get("период_суток") or None, "location": location.get("название") or None,
                    "location_id": location.get("id"), "scene": document.get("что_происходит_сейчас") or None,
                    "present_characters": [], "remote_characters": [], "remote_channels": {}, "positions": {}})
    pid = str(document.get("pov_id") or "")
    pov = deepcopy(rt.get("pov", {}))
    pov["character_id"] = pid
    characters = deepcopy(rt.get("characters", {}))
    for field, ids_key in (("физически", "present_characters"), ("удалённо", "remote_characters")):
        for row in document.get(field, []):
            cid = row["id"]
            current[ids_key].append(cid)
            info = characters.setdefault(cid, {})
            if field == "физически":
                info.update(clothing=deepcopy(row.get("одежда", "")), inventory=deepcopy(row.get("при_себе", [])), position=deepcopy(row.get("расположение", "")))
                current["positions"][cid] = deepcopy(row.get("расположение", ""))
                if cid == pid:
                    pov.update(clothing=deepcopy(info["clothing"]), inventory=deepcopy(info["inventory"]))
            else:
                current["remote_channels"][cid] = deepcopy(row.get("связь", ""))
    result.update(current=current, pov=pov, characters=characters)
    return result


def _encode_records(rows_by_kind: dict, day: str, *, memory: bool) -> dict:
    result = template("память_персонажа.json" if memory else "хронология.json")
    metadata = []
    for kind, rows in rows_by_kind.items():
        for record in rows if isinstance(rows, list) else []:
            fields = record if isinstance(record, dict) else {}
            key = next((k for k in ("text", "fact", "summary", "event", "content", "question") if fields.get(k) not in (None, "")), None)
            text = _text(record[key]) if key else json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            date = _day(fields.get("date") or day)
            if memory:
                section = "знания" if kind in {"knowledge_journal", "knowledge"} else "личные_события"
                values = result[section].setdefault(date, [])
                value = text if section == "знания" else {"с_кем": deepcopy(fields.get("participants") or fields.get("with") or []), "событие": text}
                index = len(values)
                values.append(value)
                pointer = [section, date, index]
            else:
                period = str(fields.get("period") or fields.get("period_of_day") or "события")
                values = result["дни"].setdefault(date, {}).setdefault(period, [])
                index = len(values)
                values.append(text)
                pointer = [date, period, index]
            metadata.append({"pointer": pointer, "kind": kind, "field": key,
                             "sha256": hashlib.sha256(text.encode()).hexdigest(),
                             "json_field": bool(key and not isinstance(record[key], str)),
                             "fields": _extras(fields, {key}) if key else {}})
    result["_runtime"] = {"records": metadata}
    return result


def _decode_records(document: dict, *, memory: bool) -> dict:
    rt = document.get("_runtime", {})
    metadata = {tuple(row["pointer"]): row for row in rt.get("records", [])}
    result = {kind: [] for kind in ("knowledge_journal", "knowledge", "experiences", "dialogue_memory")} if memory else {"chronology": []}
    if memory:
        groups = (([section, date, index], value, date) for section in ("знания", "личные_события")
                  for date, values in document.get(section, {}).items() for index, value in enumerate(values))
    else:
        groups = (([date, period, index], value, date) for date, periods in document.get("дни", {}).items()
                  for period, values in periods.items() for index, value in enumerate(values))
    for pointer, value, date in groups:
        text = _text(value.get("событие", "")) if isinstance(value, dict) else _text(value)
        meta = metadata.get(tuple(pointer))
        digest = hashlib.sha256(text.encode()).hexdigest()
        if meta and meta.get("sha256") == digest:
            if meta["field"] is None:
                row = json.loads(text)
            else:
                row = deepcopy(meta["fields"])
                row[meta["field"]] = json.loads(text) if meta.get("json_field") else text
            kind = meta["kind"]
        elif memory and pointer[0] == "знания":
            kind = "knowledge_journal"
            row = {"entry_id": "native_" + digest[:20], "date": date, "text": text, "turn": 0}
        elif memory:
            kind = "experiences"
            row = {"event_id": "native_" + digest[:20], "date": date, "summary": text, "participants": value.get("с_кем", []) if isinstance(value, dict) else []}
        else:
            kind = "chronology"
            row = {"event_id": "native_" + digest[:20], "date": date, "period": pointer[1], "event": text}
        result.setdefault(kind, []).append(row)
    return result


def memory_to_native(bucket: dict, cid: str, day: str) -> dict:
    result = _encode_records(bucket, day, memory=True)
    result["персонаж_id"] = cid
    result["_runtime"]["extra"] = _extras(bucket, {"knowledge_journal", "knowledge", "experiences", "dialogue_memory"})
    return result


def memory_to_internal(document: dict) -> dict:
    result = deepcopy(document.get("_runtime", {}).get("extra", {}))
    result.update(_decode_records(document, memory=True))
    return result


def relations_to_native(store: dict, existing: dict | None = None) -> dict:
    result = deepcopy(existing) if existing else template("отношения.json")
    result["pov_id"] = store.get("pov_character_id", "")
    previous = result.get("персонажи", {})
    owners = {}
    result["персонажи"] = owners
    for cid, row in store.get("npc_to_pov", {}).items():
        native = owners.setdefault(cid, deepcopy(previous.get(cid, {"к_POV": {}, "к_другим_NPC": {}})))
        relation = native.setdefault("к_POV", {})
        relation["параметры"] = {label: value.get("value", value) if isinstance(value, dict) else value for label, value in row.get("dimensions", {}).items()}
        relation.setdefault("состояние", "")
        relation.setdefault("почему", "")
        native["к_другим_NPC"] = deepcopy(store.get("npc_to_npc", {}).get(cid, {}))
    for cid, targets in store.get("npc_to_npc", {}).items():
        owners.setdefault(cid, {"к_POV": {}, "к_другим_NPC": {}})["к_другим_NPC"] = deepcopy(targets)
    result["_runtime"] = _extras(store, {"pov_character_id", "npc_to_pov", "npc_to_npc"})
    return result


def relations_to_internal(document: dict) -> dict:
    result = deepcopy(document.get("_runtime", {}))
    result.update(pov_character_id=document.get("pov_id", ""), npc_to_pov={}, npc_to_npc={})
    for cid, row in document.get("персонажи", {}).items():
        relation = row.get("к_POV", {})
        if relation:
            result["npc_to_pov"][cid] = {"dimensions": {label: {"value": value} for label, value in relation.get("параметры", {}).items()}}
        if row.get("к_другим_NPC"):
            result["npc_to_npc"][cid] = deepcopy(row["к_другим_NPC"])
    return result


def _cards(root: Path) -> list[dict]:
    roster = read(root / "персонажи.json", {}).get("персонажи", [])
    return [card_to_internal(read(root / "карточки" / (_id(row["id"]) + ".json"), {})) for row in roster]


def compat_read(path: Path, default: Any) -> tuple[bool, Any]:
    root, name = path.parent, path.name
    if not is_native(root):
        return False, None
    if name == "meta.json":
        document = read(root / "сессия.json", {})
        result = deepcopy(document.get("_runtime", {}))
        result.update(session_id=document["id"], turn_number=document["последний_ход"])
        return True, result
    if name == "state.json":
        return True, state_to_internal(read(path, default))
    if name == "characters.json":
        return True, _cards(root)
    if name == "memory.json":
        roster = read(root / "персонажи.json", {}).get("персонажи", [])
        return True, {"characters": {row["id"]: memory_to_internal(read(root / "память" / (_id(row["id"]) + ".json"), {})) for row in roster}}
    if name == "chronology.json":
        return True, _decode_records(read(root / "хронология.json", {}), memory=False)["chronology"]
    if name == "relationships.json":
        return True, relations_to_internal(read(root / "отношения.json", {}))
    if name == "source.json":
        source = read(root / ".runtime/source.json", default)
        source["novel"] = novel_to_internal(read(root / "анкета.json", {}))
        source["title"] = source["novel"].get("title") or source.get("title")
        source["locations"] = [location_to_internal(row) for row in read(root / "локации.json", {}).get("локации", [])]
        lore = read(root / "скрытый_лор_и_крючки.json", {})
        source["hidden_lore"] = deepcopy(lore.get("_runtime", {}).get("hidden_lore", {"entries": lore.get("скрытый_лор", [])}))
        return True, source
    if name in _RENAMED:
        return True, read(file_path(root, name), default)
    return False, None


def document_exists(path: Path) -> bool:
    root, name = path.parent, path.name
    if is_native(root) and name in _VIRTUAL:
        if name in {"characters.json", "memory.json"}:
            return (root / "персонажи.json").is_file()
        mapped = {"meta.json": "сессия.json", "chronology.json": "хронология.json", "relationships.json": "отношения.json"}
        return (root / mapped.get(name, _RENAMED.get(name, name))).is_file()
    return file_path(root, name).is_file()


def _eligible(source: dict) -> bool:
    try:
        return int(source.get("version", 1) or 1) >= 5 or isinstance(source.get("profile_schema"), dict)
    except (ValueError, TypeError):
        return isinstance(source.get("profile_schema"), dict)


def encode_batch(root: Path, values: dict[str, str]) -> dict[str, str | None]:
    source = json.loads(values["source.json"]) if "source.json" in values else {}
    if not is_native(root) and not _eligible(source):
        return values
    result = {(_RENAMED.get(name, name)): text for name, text in values.items() if name not in _VIRTUAL}
    parsed = {name: json.loads(text) for name, text in values.items() if name in _VIRTUAL}
    def put(name: str, data: Any):
        result[name] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if source:
        put(".runtime/source.json", source)
        put("анкета.json", novel_to_native(source))
        locations = template("локации.json")
        locations["локации"] = [location_to_native(row) for row in source.get("locations", [])]
        put("локации.json", locations)
        lore = template("скрытый_лор_и_крючки.json")
        hidden = source.get("hidden_lore", {})
        entries = hidden.get("entries", []) if isinstance(hidden, dict) else hidden if isinstance(hidden, list) else []
        lore["скрытый_лор"] = [{"суть": _text(row), "появилось": "старт", "связанные_id": []} for row in entries]
        lore["_runtime"] = {"hidden_lore": deepcopy(hidden)}
        put("скрытый_лор_и_крючки.json", lore)
    if "meta.json" in parsed:
        meta = parsed["meta.json"]
        document = template("сессия.json")
        document.update(id=meta["session_id"], последний_ход=int(meta.get("turn_number", 0)))
        document["этап"] = "игра" if document["последний_ход"] else "готово_к_игре"
        document["_runtime"] = _extras(meta, {"session_id", "turn_number"})
        put("сессия.json", document)
    state = parsed.get("state.json")
    current_native = read(root / "state.json", {})
    if state is not None:
        native = state_to_native(state, current_native if "pov_id" in current_native else None)
        put("state.json", native)
    else:
        state = state_to_internal(current_native) if current_native else {}
    day = state.get("current", {}).get("date") or "без даты"
    if "characters.json" in parsed:
        roster = template("персонажи.json")
        existing_roster = {row["id"]: row for row in read(root / "персонажи.json", {}).get("персонажи", [])}
        source_ids = {str(c.get("character_id") or c.get("id") or c.get("name")) for c in source.get("characters", [])}
        if not source_ids:
            original = read(root / ".runtime/source.json", {})
            source_ids = {str(c.get("character_id") or c.get("id") or c.get("name")) for c in original.get("characters", [])}
        for card in parsed["characters.json"]:
            cid = _id(card.get("character_id") or card.get("id") or card.get("name"))
            old = read(root / "карточки" / (cid + ".json"), {})
            put("карточки/" + cid + ".json", card_to_native(card, old or None))
            row = deepcopy(existing_roster.get(cid, {"id": cid, "стартовый": cid in source_ids}))
            row["имя"] = " ".join(str(card.get(k) or "") for k in ("name", "surname")).strip()
            row["роль_в_сюжете"] = card.get("story_function") or card.get("role") or ""
            info = state.get("characters", {}).get(cid, {})
            row["где_сейчас"] = info.get("location") or row.get("где_сейчас", "")
            row["что_делает"] = info.get("activity") or row.get("что_делает", "")
            row.setdefault("последняя_встреча", "")
            row.setdefault("последнее_событие_с_POV", {"дата": "", "событие": ""})
            roster["персонажи"].append(row)
        retained_ids = {row["id"] for row in roster["персонажи"]}
        for cid in set(existing_roster) - retained_ids:
            result["карточки/" + _id(cid) + ".json"] = None
            result["память/" + _id(cid) + ".json"] = None
        put("персонажи.json", roster)
    if "memory.json" in parsed:
        memory = parsed["memory.json"]
        for cid, bucket in memory.get("characters", memory).items():
            put("память/" + _id(cid) + ".json", memory_to_native(bucket, cid, day))
    if "chronology.json" in parsed:
        put("хронология.json", _encode_records({"chronology": parsed["chronology.json"]}, day, memory=False))
    if "relationships.json" in parsed:
        put("отношения.json", relations_to_native(parsed["relationships.json"], read(root / "отношения.json", {}) or None))
    return result


def ensure_native(root: Path) -> bool:
    """Move V5 stories once, keeping the same session ID and exact source/archive."""
    from .transactional_storage import session_transaction, write_batch
    with session_transaction(root):
        if is_native(root):
            return False
        source = read(root / "source.json", {})
        if not _eligible(source):
            return False
        values = {name: path.read_text(encoding="utf-8") for name in (*_VIRTUAL, "turns.jsonl", "audits.json")
                  if (path := root / name).is_file()}
        write_batch(root, values)
        # The transaction has committed the authoritative files. Old aggregates
        # are now redundant; no originals, snapshots or scene history are lost.
        for name in (*_VIRTUAL, "turns.jsonl", "audits.json"):
            if name != "state.json":
                (root / name).unlink(missing_ok=True)
        return True


def writer_documents(root: Path, participants: list[str]) -> dict:
    """Read the full chronology and complete dated memory of actual participants."""
    from fastapi import HTTPException
    def required(name: str) -> dict:
        path = root / name
        if not path.is_file():
            raise HTTPException(409, {"code": "SESSION_DOCUMENT_REQUIRED", "path": name,
                "message": "Cannot write a scene without its chronology and participant knowledge."})
        value = read(path)
        if not isinstance(value, dict):
            raise HTTPException(409, {"code": "SESSION_DOCUMENT_INVALID", "path": name})
        return value
    from .personal_memory_transport import personal_dialogue_record
    cards = _cards(root)
    memories = {}
    for cid in participants:
        raw = required("память/" + _id(cid) + ".json")
        memory = public(raw)
        for row in raw.get("_runtime", {}).get("records", []):
            if row.get("kind") != "dialogue_memory" or row.get("field") is None:
                continue
            section, day, index = row["pointer"]
            values = memory.get(section, {}).get(day, [])
            if not 0 <= index < len(values) or not isinstance(values[index], dict):
                continue
            event = values[index]
            if hashlib.sha256(str(event.get("событие", "")).encode()).hexdigest() != row.get("sha256"):
                continue
            original = deepcopy(row["fields"])
            original[row["field"]] = event.get("событие", "")
            safe = personal_dialogue_record(original, owner_id=cid, cards=cards)
            event["с_кем"] = []  # IDs in a technical dialogue receipt never reveal names.
            event["событие"] = safe.get("summary") or safe.get("content") or safe.get("question") or event["событие"]
            for key in ("segments", "question", "answer", "content"):
                if key in safe and safe[key] not in (None, "", []):
                    event[key] = deepcopy(safe[key])
        memories[cid] = memory
    return {
        "chronology": public(required("хронология.json")),
        "personal_memory": memories,
    }
