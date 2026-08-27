import json
import os
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT_DIR / "state_templates"
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY_DIR = DATA_DIR / "library"
SESSIONS_DIR = DATA_DIR / "sessions"
MAX_PACKET_CHARS = 12000


def ensure_dirs() -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _template(name: str, default: Any) -> Any:
    return _read_json(TEMPLATE_DIR / name, default)


def _deep_merge(target: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(target)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _read_turns(root: Path) -> List[Dict[str, Any]]:
    path = root / "turns.jsonl"
    if not path.exists():
        return []
    turns: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            turns.append(json.loads(line))
    return turns


def _normalise_memory(memory: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(memory.get("characters"), dict):
        return memory
    legacy = {k: v for k, v in memory.items() if isinstance(v, dict)}
    return {"characters": legacy}


def _memory_bucket(memory: Dict[str, Any], character_id: str) -> Dict[str, List[Dict[str, Any]]]:
    memory.setdefault("characters", {})
    bucket = memory["characters"].setdefault(character_id, {})
    bucket.setdefault("knowledge", [])
    bucket.setdefault("experiences", [])
    bucket.setdefault("dialogue_memory", [])
    return bucket


def _upsert_by_id(items: List[Dict[str, Any]], item: Dict[str, Any], id_key: str) -> None:
    item_id = item.get(id_key)
    if not item_id:
        return
    for index, existing in enumerate(items):
        if existing.get(id_key) == item_id:
            items[index] = _deep_merge(existing, item)
            return
    items.append(deepcopy(item))


def _apply_memory_events(memory: Dict[str, Any], extracted: Dict[str, Any], turn_number: int) -> Dict[str, Any]:
    result = _normalise_memory(deepcopy(memory))
    for item in extracted.get("knowledge_add", []) if isinstance(extracted.get("knowledge_add"), list) else []:
        character_id = item.get("character_id")
        if not character_id:
            continue
        record = deepcopy(item)
        record.setdefault("learned_turn", turn_number)
        record.setdefault("confidence", "certain")
        _upsert_by_id(_memory_bucket(result, str(character_id))["knowledge"], record, "fact_id")
    for item in extracted.get("experiences_add", []) if isinstance(extracted.get("experiences_add"), list) else []:
        character_id = item.get("character_id")
        if not character_id:
            continue
        record = deepcopy(item)
        record.setdefault("turn", turn_number)
        _upsert_by_id(_memory_bucket(result, str(character_id))["experiences"], record, "event_id")
    for item in extracted.get("dialogue_memory_add", []) if isinstance(extracted.get("dialogue_memory_add"), list) else []:
        participants = item.get("participants") or []
        if isinstance(participants, str):
            participants = [participants]
        for character_id in participants:
            record = deepcopy(item)
            record.setdefault("turn", turn_number)
            _upsert_by_id(_memory_bucket(result, str(character_id))["dialogue_memory"], record, "topic_id")
    return result


def _card_id(card: Dict[str, Any]) -> str:
    return str(card.get("character_id") or card.get("id") or card.get("name") or "").strip()


def _card_name(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("name") or card.get("full_name") or identity.get("name") or _card_id(card)).strip()


def _card_names(card: Dict[str, Any]) -> List[str]:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    values: List[str] = []
    for value in (
        card.get("character_id"), card.get("id"), card.get("name"), card.get("full_name"),
        card.get("short_name"), identity.get("name"), identity.get("full_name"),
    ):
        if value:
            values.append(str(value))
    aliases = card.get("aliases") or identity.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    values.extend(str(v) for v in aliases if v)
    return list(dict.fromkeys(values))


def _card_role(card: Dict[str, Any]) -> Any:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return card.get("role") or card.get("story_role") or identity.get("role")


def _normalise_cards(cards: Any) -> List[Dict[str, Any]]:
    if not isinstance(cards, list):
        return []
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        card = deepcopy(raw)
        cid = _card_id(card)
        if not cid or cid in seen:
            continue
        card.setdefault("character_id", cid)
        result.append(card)
        seen.add(cid)
    return result


def _find_pov_id(novel: Dict[str, Any], cards: List[Dict[str, Any]]) -> str | None:
    starting = novel.get("starting_state") if isinstance(novel.get("starting_state"), dict) else {}
    pov_state = starting.get("pov") if isinstance(starting.get("pov"), dict) else {}
    novel_info = novel.get("novel") if isinstance(novel.get("novel"), dict) else {}
    candidates = [
        pov_state.get("character_id"), starting.get("pov_character_id"), starting.get("pov_character"),
        novel_info.get("pov_character_id"), novel_info.get("pov_character"), novel_info.get("pov"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if not value:
            continue
        needle = str(value).casefold()
        for card in cards:
            if _card_id(card).casefold() == needle or any(name.casefold() == needle for name in _card_names(card)):
                return _card_id(card)
    for card in cards:
        if card.get("is_pov") is True or str(card.get("type", "")).casefold() == "pov":
            return _card_id(card)
    return None


def _load_cards(root: Path, source: Dict[str, Any]) -> List[Dict[str, Any]]:
    stored = _read_json(root / "characters.json", None)
    if isinstance(stored, list):
        return _normalise_cards(stored)
    return _normalise_cards(source.get("characters", []))


def _present_character_ids(state: Dict[str, Any]) -> List[str]:
    result: List[str] = []
    present = state.get("current", {}).get("present_characters", [])
    if isinstance(present, str):
        present = [present]
    for value in present if isinstance(present, list) else []:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.append(str(value))
    for cid, info in (state.get("characters", {}) or {}).items():
        if isinstance(info, dict) and info.get("present") is True:
            result.append(str(cid))
    pov_id = state.get("pov", {}).get("character_id")
    if pov_id:
        result.append(str(pov_id))
    return list(dict.fromkeys(result))


def _thread_character_ids(state: Dict[str, Any]) -> List[str]:
    result: List[str] = []
    threads = state.get("threads", {})
    values = threads.values() if isinstance(threads, dict) else threads if isinstance(threads, list) else []
    for thread in values:
        if not isinstance(thread, dict):
            continue
        participants = thread.get("character_ids") or thread.get("participants") or []
        if isinstance(participants, str):
            participants = [participants]
        result.extend(str(x) for x in participants if x)
    return result


def _relevant_character_ids(cards: List[Dict[str, Any]], state: Dict[str, Any], user_input: str) -> List[str]:
    selected = _present_character_ids(state)
    selected.extend(_thread_character_ids(state))
    text = user_input.casefold()
    for card in cards:
        cid = _card_id(card)
        if cid and any(name.casefold() in text for name in _card_names(card) if len(name.strip()) >= 2):
            selected.append(cid)
    valid = {_card_id(card) for card in cards}
    return [cid for cid in dict.fromkeys(selected) if cid in valid]


def _relationship_hint(state: Dict[str, Any], character_id: str) -> Any:
    relationships = state.get("relationships", {})
    if not isinstance(relationships, dict):
        return None
    return relationships.get(character_id) or relationships.get(f"{character_id}->pov")


def _cast_index(cards: List[Dict[str, Any]], state: Dict[str, Any], current_turn: int) -> List[Dict[str, Any]]:
    runtime = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    present = set(_present_character_ids(state))
    result: List[Dict[str, Any]] = []
    for card in cards:
        cid = _card_id(card)
        info = runtime.get(cid, {}) if isinstance(runtime.get(cid), dict) else {}
        item = {
            "character_id": cid,
            "name": _card_name(card),
            "role": _card_role(card),
            "status": info.get("status") or card.get("status") or "active",
            "present": cid in present,
            "location": info.get("location"),
            "last_seen_turn": info.get("last_seen_turn"),
            "relationship_to_pov": _relationship_hint(state, cid),
        }
        result.append({k: v for k, v in item.items() if v is not None})
    return result


def _apply_character_upserts(cards: List[Dict[str, Any]], extracted: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = _normalise_cards(cards)
    index = {_card_id(card): i for i, card in enumerate(result)}
    upserts = extracted.get("character_upserts", [])
    if not isinstance(upserts, list):
        return result
    for raw in upserts:
        if not isinstance(raw, dict):
            continue
        cid = _card_id(raw)
        if not cid:
            continue
        card = deepcopy(raw)
        card.setdefault("character_id", cid)
        if cid in index:
            result[index[cid]] = _deep_merge(result[index[cid]], card)
        else:
            index[cid] = len(result)
            result.append(card)
    return result


def _refresh_runtime_presence(state: Dict[str, Any], cards: List[Dict[str, Any]], turn_number: int) -> Dict[str, Any]:
    state = deepcopy(state)
    state.setdefault("characters", {})
    current = state.setdefault("current", {})
    present = current.get("present_characters", [])
    if isinstance(present, str):
        present = [present]
    present_ids = set(str(x.get("character_id") or x.get("id") or x.get("name")) if isinstance(x, dict) else str(x) for x in present if x)
    location = current.get("location")
    for card in cards:
        cid = _card_id(card)
        info = state["characters"].setdefault(cid, {})
        if cid in present_ids:
            info["present"] = True
            info["last_seen_turn"] = turn_number
            if location is not None:
                info["location"] = location
        elif info.get("present") is True:
            info["present"] = False
    return state


def list_novels() -> List[Dict[str, Any]]:
    ensure_dirs()
    result = []
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        data = _read_json(path, {})
        result.append({"novel_id": data.get("novel_id"), "title": data.get("title"), "version": data.get("version", 1)})
    return result


def save_novel(template: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    _write_json(LIBRARY_DIR / f"{template['novel_id']}.json", template)
    return template


def get_novel(novel_id: str) -> Dict[str, Any]:
    path = LIBRARY_DIR / f"{novel_id}.json"
    if not path.exists():
        raise FileNotFoundError(novel_id)
    return _read_json(path, {})


def create_session(novel: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    session_id = uuid.uuid4().hex
    root = SESSIONS_DIR / session_id
    root.mkdir(parents=True, exist_ok=False)
    meta = _template("session_meta.json", {})
    meta.update({"session_id": session_id, "source_novel_id": novel["novel_id"], "source_novel_version": novel.get("version", 1)})

    cards = _normalise_cards(novel.get("characters", []))
    state = _template("state.json", {"current": {}, "pov": {}, "characters": {}, "relationships": {}, "threads": {}, "world": {}})
    starting_state = novel.get("starting_state") if isinstance(novel.get("starting_state"), dict) else {}
    state = _deep_merge(state, starting_state)
    pov_id = _find_pov_id(novel, cards)
    if pov_id and not state.get("pov", {}).get("character_id"):
        state.setdefault("pov", {})["character_id"] = pov_id
    if isinstance(novel.get("world"), dict):
        state["world"] = _deep_merge(novel.get("world", {}), state.get("world", {}))

    memory = _template("memory.json", {"characters": {}})
    memory = _normalise_memory(memory)
    for card in cards:
        _memory_bucket(memory, _card_id(card))

    chronology = _template("chronology.json", [])
    audits = _template("audits.json", [])
    state = _refresh_runtime_presence(state, cards, 0)

    _write_json(root / "meta.json", meta)
    _write_json(root / "source.json", novel)
    _write_json(root / "characters.json", cards)
    _write_json(root / "state.json", state)
    _write_json(root / "memory.json", memory)
    _write_json(root / "chronology.json", chronology)
    _write_json(root / "audits.json", audits)
    (root / "turns.jsonl").write_text("", encoding="utf-8")
    return meta


def load_session(session_id: str, recent_limit: int = 6) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    source = _read_json(root / "source.json", {})
    turns = _read_turns(root)
    return {
        "meta": _read_json(root / "meta.json", {}),
        "source": source,
        "characters": _load_cards(root, source),
        "state": _read_json(root / "state.json", {}),
        "memory": _normalise_memory(_read_json(root / "memory.json", {})),
        "chronology": _read_json(root / "chronology.json", []),
        "recent_turns": turns[-recent_limit:],
    }


def get_character_memory(session_id: str, character_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    return _memory_bucket(memory, character_id)


def get_turn_range(session_id: str, start_turn: int, end_turn: int) -> List[Dict[str, Any]]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return [t for t in _read_turns(root) if start_turn <= int(t.get("turn_number", 0)) <= end_turn]


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    end_turn = int(meta.get("turn_number", 0))
    start_turn = max(int(meta.get("last_audit_turn", 0)) + 1, end_turn - 14)
    state = _read_json(root / "state.json", {})
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    chronology = _read_json(root / "chronology.json", [])
    recent_memory: Dict[str, Any] = {}
    for character_id, bucket in memory.get("characters", {}).items():
        if not isinstance(bucket, dict):
            continue
        knowledge = [x for x in bucket.get("knowledge", []) if int(x.get("learned_turn", 0) or 0) >= start_turn]
        experiences = [x for x in bucket.get("experiences", []) if int(x.get("turn", 0) or 0) >= start_turn]
        dialogue = [x for x in bucket.get("dialogue_memory", []) if int(x.get("turn", 0) or 0) >= start_turn]
        if knowledge or experiences or dialogue:
            recent_memory[character_id] = {"knowledge": knowledge, "experiences": experiences, "dialogue_memory": dialogue}
    recent_chronology = []
    if isinstance(chronology, list):
        for item in chronology:
            if not isinstance(item, dict):
                continue
            turn = item.get("turn") or item.get("turn_number")
            if turn is None or int(turn or 0) >= start_turn:
                recent_chronology.append(item)
        recent_chronology = recent_chronology[-40:]
    return {
        "audit_range": [start_turn, end_turn],
        "state": state,
        "saved_this_cycle": {"chronology": recent_chronology, "memory": recent_memory},
        "instruction": "FAST AUDIT. Use the last 15 turns already visible in the current chat. Do not fetch raw turns. Add only missing chronology, character knowledge/experience/dialogue memory, and obvious state corrections. Then call commitAudit once.",
    }


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        raise RuntimeError("AUDIT_REQUIRED")
    if meta.get("handoff_required"):
        raise RuntimeError("HANDOFF_REQUIRED")

    source = _read_json(root / "source.json", {})
    cards = _load_cards(root, source)
    state = _read_json(root / "state.json", {})
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    chronology = _read_json(root / "chronology.json", [])
    turns = _read_turns(root)
    relevant_ids = _relevant_character_ids(cards, state, user_input)

    card_map = {_card_id(card): card for card in cards}
    relevant_cards = [card_map[cid] for cid in relevant_ids if cid in card_map]
    relevant_memory = {cid: _memory_bucket(memory, cid) for cid in relevant_ids}

    context = {
        "packet_version": 2,
        "session": meta,
        "expected_turn": int(meta.get("turn_number", 0)) + 1,
        "user_input": user_input,
        "scene_state": state,
        "novel": source.get("novel", {}),
        "novel_rules": source.get("rules", {}),
        "novel_lore": source.get("lore", {}),
        "hidden_lore": source.get("hidden_lore", {}),
        "story_direction": source.get("story_direction", {}),
        "world_canon": source.get("world", {}),
        "cast_index": _cast_index(cards, state, int(meta.get("turn_number", 0))),
        "relevant_character_ids": relevant_ids,
        "character_cards": relevant_cards,
        "character_memory": relevant_memory,
        "active_threads": state.get("threads", {}),
        "relationships": state.get("relationships", {}),
        "chronology_recent": chronology[-30:] if isinstance(chronology, list) else chronology,
        "recent_turns": turns[-6:],
    }
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + MAX_PACKET_CHARS] for i in range(0, len(text), MAX_PACKET_CHARS)] or ["{}"]
    packet = {
        "packet_id": secrets.token_urlsafe(12),
        "prepared_for_turn": int(meta.get("turn_number", 0)) + 1,
        "user_input": user_input,
        "relevant_character_ids": relevant_ids,
        "chunk_count": len(chunks),
        "read_chunks": [],
        "chunks": chunks,
    }
    _write_json(root / "turn_packet.json", packet)
    return {
        "packet_id": packet["packet_id"],
        "prepared_for_turn": packet["prepared_for_turn"],
        "chunk_count": packet["chunk_count"],
        "relevant_character_ids": relevant_ids,
        "instruction": "Read every chunk from 0 through chunk_count-1 before writing or committing the scene. cast_index is the compact roster; full cards and personal memory are included for scene-relevant characters.",
    }


def get_turn_packet_chunk(session_id: str, packet_id: str, chunk_index: int) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    packet = _read_json(root / "turn_packet.json", {})
    if not packet or packet.get("packet_id") != packet_id:
        raise PermissionError("INVALID_PACKET")
    chunks = packet.get("chunks", [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")
    read_chunks = set(packet.get("read_chunks", []))
    read_chunks.add(chunk_index)
    packet["read_chunks"] = sorted(read_chunks)
    _write_json(root / "turn_packet.json", packet)
    return {"packet_id": packet_id, "chunk_index": chunk_index, "chunk_count": len(chunks), "content": chunks[chunk_index], "all_chunks_read": len(read_chunks) == len(chunks)}


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        raise RuntimeError("AUDIT_REQUIRED")
    if meta.get("handoff_required"):
        raise RuntimeError("HANDOFF_REQUIRED")
    turn_number = int(meta.get("turn_number", 0)) + 1
    packet = _read_json(root / "turn_packet.json", {})
    if not packet or packet.get("prepared_for_turn") != turn_number or packet.get("user_input") != payload.get("user_input"):
        raise RuntimeError("TURN_PACKET_REQUIRED")
    if len(set(packet.get("read_chunks", []))) < int(packet.get("chunk_count", 0)):
        raise RuntimeError("TURN_PACKET_INCOMPLETE")

    extracted = payload.get("extracted", {}) if isinstance(payload.get("extracted"), dict) else {}
    entry = _template("turn.json", {})
    entry.update({"turn_number": turn_number, "saved_at": datetime.now(timezone.utc).isoformat(), "user_input": payload["user_input"], "scene_output": payload["scene_output"], "extracted": extracted})
    with (root / "turns.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    source = _read_json(root / "source.json", {})
    cards = _apply_character_upserts(_load_cards(root, source), extracted)
    _write_json(root / "characters.json", cards)

    state = _read_json(root / "state.json", {})
    if isinstance(extracted.get("state_patch"), dict):
        state = _deep_merge(state, extracted["state_patch"])
    state = _refresh_runtime_presence(state, cards, turn_number)
    _write_json(root / "state.json", state)

    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    for card in cards:
        _memory_bucket(memory, _card_id(card))
    memory = _apply_memory_events(memory, extracted, turn_number)
    _write_json(root / "memory.json", memory)

    chronology = _read_json(root / "chronology.json", [])
    if isinstance(extracted.get("chronology"), list):
        chronology.extend(extracted["chronology"])
        _write_json(root / "chronology.json", chronology)

    meta["turn_number"] = turn_number
    audit_due = turn_number % 15 == 0
    handoff_due = turn_number % 60 == 0
    if audit_due:
        meta["audit_required"] = True
    if handoff_due:
        meta["handoff_required"] = True
        _write_json(root / "handoff_tail.json", get_turn_range(session_id, max(1, turn_number - 5), turn_number))
    _write_json(root / "meta.json", meta)
    packet_path = root / "turn_packet.json"
    if packet_path.exists():
        packet_path.unlink()
    return {"ok": True, "turn_number": turn_number, "audit_due": audit_due, "audit_range": [max(1, turn_number - 14), turn_number] if audit_due else None, "handoff_required": handoff_due}


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    expected_end = int(meta.get("turn_number", 0))
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    if int(payload.get("end_turn", 0)) != expected_end:
        raise ValueError("AUDIT_RANGE_MISMATCH")
    repairs = payload.get("repairs", {}) if isinstance(payload.get("repairs"), dict) else {}
    state = _read_json(root / "state.json", {})
    if isinstance(repairs.get("state_patch"), dict):
        state = _deep_merge(state, repairs["state_patch"])
        _write_json(root / "state.json", state)
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    memory = _apply_memory_events(memory, repairs, expected_end)
    _write_json(root / "memory.json", memory)
    chronology = _read_json(root / "chronology.json", [])
    if isinstance(repairs.get("chronology_add"), list):
        chronology.extend(repairs["chronology_add"])
        _write_json(root / "chronology.json", chronology)
    audits = _read_json(root / "audits.json", [])
    audits.append({"start_turn": payload["start_turn"], "end_turn": payload["end_turn"], "repairs": repairs, "notes": payload.get("notes", []), "saved_at": datetime.now(timezone.utc).isoformat()})
    _write_json(root / "audits.json", audits)
    meta["last_audit_turn"] = payload["end_turn"]
    meta["audit_required"] = False
    _write_json(root / "meta.json", meta)
    return {"ok": True, "audited_through": payload["end_turn"], "handoff_required": bool(meta.get("handoff_required"))}


def build_resume_package(session_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        raise RuntimeError("AUDIT_REQUIRED")
    if not meta.get("handoff_required"):
        raise RuntimeError("HANDOFF_NOT_REQUIRED")
    token = secrets.token_urlsafe(24)
    _write_json(root / "resume_token.json", {"token": token})
    source = _read_json(root / "source.json", {})
    return {
        "session_id": session_id,
        "resume_token": token,
        "meta": meta,
        "source": source,
        "characters": _load_cards(root, source),
        "state": _read_json(root / "state.json", {}),
        "memory": _normalise_memory(_read_json(root / "memory.json", {})),
        "chronology": _read_json(root / "chronology.json", []),
        "handoff_tail": _read_json(root / "handoff_tail.json", []),
        "instruction": "Restore this exact session. Source is immutable starting canon; characters is the live card registry including NPCs created after start; state is current truth; memory is personal character memory; chronology is persistent history; handoff_tail is exact recent scene continuity.",
    }


def confirm_resume(session_id: str, resume_token: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    saved = _read_json(root / "resume_token.json", {})
    if not saved or saved.get("token") != resume_token:
        raise PermissionError("INVALID_RESUME_TOKEN")
    meta = _read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        raise RuntimeError("AUDIT_REQUIRED")
    meta["handoff_required"] = False
    meta["handoff_generation"] = int(meta.get("handoff_generation", 0)) + 1
    meta["last_resumed_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(root / "meta.json", meta)
    for name in ("handoff_tail.json", "resume_token.json", "turn_packet.json"):
        path = root / name
        if path.exists():
            path.unlink()
    return {"ok": True, "session_id": session_id, "turn_number": meta.get("turn_number"), "handoff_generation": meta["handoff_generation"]}
