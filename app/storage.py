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
    turns = []
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
        _upsert_by_id(_memory_bucket(result, character_id)["knowledge"], record, "fact_id")
    for item in extracted.get("experiences_add", []) if isinstance(extracted.get("experiences_add"), list) else []:
        character_id = item.get("character_id")
        if not character_id:
            continue
        record = deepcopy(item)
        record.setdefault("turn", turn_number)
        _upsert_by_id(_memory_bucket(result, character_id)["experiences"], record, "event_id")
    for item in extracted.get("dialogue_memory_add", []) if isinstance(extracted.get("dialogue_memory_add"), list) else []:
        participants = item.get("participants") or []
        if isinstance(participants, str):
            participants = [participants]
        for character_id in participants:
            record = deepcopy(item)
            record.setdefault("turn", turn_number)
            _upsert_by_id(_memory_bucket(result, character_id)["dialogue_memory"], record, "topic_id")
    return result


def _card_id(card: Dict[str, Any]) -> str:
    return str(card.get("character_id") or card.get("id") or card.get("name") or "").strip()


def _card_names(card: Dict[str, Any]) -> List[str]:
    values = []
    for key in ("character_id", "id", "name", "full_name", "short_name"):
        value = card.get(key)
        if value:
            values.append(str(value))
    aliases = card.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    values.extend(str(v) for v in aliases if v)
    return values


def _present_character_ids(state: Dict[str, Any]) -> List[str]:
    result = []
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


def _relevant_character_ids(source: Dict[str, Any], state: Dict[str, Any], user_input: str) -> List[str]:
    cards = source.get("characters", []) if isinstance(source.get("characters"), list) else []
    selected = _present_character_ids(state)
    text = user_input.casefold()
    for card in cards:
        cid = _card_id(card)
        if not cid:
            continue
        if any(name.casefold() in text for name in _card_names(card) if len(name.strip()) >= 2):
            selected.append(cid)
    return list(dict.fromkeys(selected))


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
    meta.update({
        "session_id": session_id,
        "source_novel_id": novel["novel_id"],
        "source_novel_version": novel.get("version", 1),
    })
    state = _template("state.json", {"current": {}, "pov": {}, "characters": {}, "relationships": {}, "threads": {}, "world": {}})
    memory = _template("memory.json", {"characters": {}})
    chronology = _template("chronology.json", [])
    audits = _template("audits.json", [])
    _write_json(root / "meta.json", meta)
    _write_json(root / "source.json", novel)
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
    turns = _read_turns(root)
    return {
        "meta": _read_json(root / "meta.json", {}),
        "source": _read_json(root / "source.json", {}),
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
    state = _read_json(root / "state.json", {})
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    chronology = _read_json(root / "chronology.json", [])
    turns = _read_turns(root)
    relevant_ids = _relevant_character_ids(source, state, user_input)
    cards = []
    relevant_memory = {}
    for card in source.get("characters", []) if isinstance(source.get("characters"), list) else []:
        cid = _card_id(card)
        if cid in relevant_ids:
            cards.append(card)
            relevant_memory[cid] = _memory_bucket(memory, cid)

    context = {
        "packet_version": 1,
        "session": meta,
        "expected_turn": int(meta.get("turn_number", 0)) + 1,
        "user_input": user_input,
        "scene_state": state,
        "novel": source.get("novel", {}),
        "novel_lore": source.get("lore", {}),
        "relevant_character_ids": relevant_ids,
        "character_cards": cards,
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
        "instruction": "Read every chunk from 0 through chunk_count-1 before writing or committing the scene.",
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
    return {
        "packet_id": packet_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": len(read_chunks) == len(chunks),
    }


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

    extracted = payload.get("extracted", {})
    entry = _template("turn.json", {})
    entry.update({
        "turn_number": turn_number,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "user_input": payload["user_input"],
        "scene_output": payload["scene_output"],
        "extracted": extracted,
    })
    with (root / "turns.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    state = _read_json(root / "state.json", {})
    if isinstance(extracted.get("state_patch"), dict):
        state = _deep_merge(state, extracted["state_patch"])
        _write_json(root / "state.json", state)

    memory = _normalise_memory(_read_json(root / "memory.json", {}))
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
        tail = get_turn_range(session_id, max(1, turn_number - 5), turn_number)
        _write_json(root / "handoff_tail.json", tail)
    _write_json(root / "meta.json", meta)
    packet_path = root / "turn_packet.json"
    if packet_path.exists():
        packet_path.unlink()

    return {
        "ok": True,
        "turn_number": turn_number,
        "audit_due": audit_due,
        "audit_range": [max(1, turn_number - 14), turn_number] if audit_due else None,
        "handoff_required": handoff_due,
    }


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
    repairs = payload.get("repairs", {})
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
    audits.append({
        "start_turn": payload["start_turn"],
        "end_turn": payload["end_turn"],
        "repairs": repairs,
        "notes": payload.get("notes", []),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })
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
    return {
        "session_id": session_id,
        "resume_token": token,
        "meta": meta,
        "source": _read_json(root / "source.json", {}),
        "state": _read_json(root / "state.json", {}),
        "memory": _normalise_memory(_read_json(root / "memory.json", {})),
        "chronology": _read_json(root / "chronology.json", []),
        "handoff_tail": _read_json(root / "handoff_tail.json", []),
        "instruction": "Restore this exact session. Source is canon, state is current truth, memory contains each character's knowledge/experience/dialogue history, chronology is persistent history, and handoff_tail is exact recent scene continuity. Never replace these with guesses.",
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
