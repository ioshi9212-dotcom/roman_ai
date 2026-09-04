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
# Keep individual transport chunks small enough that a fixed two-chunk Action batch
# stays comfortably below the ChatGPT Actions response-size ceiling.
MAX_PACKET_CHARS = 6000


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


def _source_characters(novel: Dict[str, Any]) -> List[Dict[str, Any]]:
    chars = novel.get("characters")
    if isinstance(chars, list):
        return [deepcopy(x) for x in chars if isinstance(x, dict)]
    return []


def _card_id(card: Dict[str, Any]) -> str:
    value = card.get("character_id") or card.get("id")
    if value:
        return str(value)
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    value = identity.get("character_id") or identity.get("id")
    if value:
        return str(value)
    return ""


def _card_name(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("name") or card.get("full_name") or identity.get("name") or _card_id(card))


def _card_role(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("role") or identity.get("role") or "")


def _card_names(card: Dict[str, Any]) -> List[str]:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    values = [
        _card_id(card),
        card.get("name"),
        card.get("full_name"),
        identity.get("name"),
        identity.get("full_name"),
    ]
    aliases = card.get("aliases")
    if isinstance(aliases, list):
        values.extend(aliases)
    return [str(value) for value in values if value not in (None, "")]


def _normalise_memory(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    result = deepcopy(value)
    if not isinstance(result.get("characters"), dict):
        result["characters"] = {}
    return result


def _memory_bucket(memory: Dict[str, Any], character_id: str) -> Dict[str, Any]:
    characters = memory.setdefault("characters", {})
    bucket = characters.get(character_id)
    if not isinstance(bucket, dict):
        bucket = {}
        characters[character_id] = bucket
    for key in ("knowledge", "experiences", "dialogue_memory"):
        if not isinstance(bucket.get(key), list):
            bucket[key] = []
    return bucket


def _read_turns(root: Path) -> List[Dict[str, Any]]:
    path = root / "turns.jsonl"
    if not path.exists():
        return []
    result: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return deepcopy(patch)
    result = deepcopy(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _present_character_ids(state: Dict[str, Any]) -> List[str]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    values = current.get("present_characters") or current.get("present_character_ids") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    result: List[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.append(str(value))
    return list(dict.fromkeys(result))


def _load_cards(root: Path, source: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards = _read_json(root / "characters.json", None)
    if isinstance(cards, list):
        return [deepcopy(x) for x in cards if isinstance(x, dict)]
    return _source_characters(source)


def _apply_character_upserts(cards: List[Dict[str, Any]], extracted: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = extracted.get("character_upserts")
    if not isinstance(values, list):
        return cards
    result = [deepcopy(card) for card in cards]
    index = {_card_id(card): pos for pos, card in enumerate(result) if _card_id(card)}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        cid = _card_id(item)
        if not cid:
            continue
        item["character_id"] = cid
        if cid in index:
            result[index[cid]] = _deep_merge(result[index[cid]], item)
        else:
            index[cid] = len(result)
            result.append(item)
    return result


def _apply_memory_events(memory: Dict[str, Any], extracted: Dict[str, Any], turn_number: int) -> Dict[str, Any]:
    result = _normalise_memory(memory)
    for field, bucket_key in (("knowledge_add", "knowledge"), ("experiences_add", "experiences")):
        values = extracted.get(field)
        if not isinstance(values, list):
            continue
        for raw in values:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("character_id") or raw.get("owner_character_id") or "")
            if not cid:
                continue
            item = deepcopy(raw)
            if bucket_key == "knowledge":
                item.setdefault("learned_turn", turn_number)
            else:
                item.setdefault("turn", turn_number)
            _memory_bucket(result, cid)[bucket_key].append(item)

    values = extracted.get("dialogue_memory_add")
    if isinstance(values, list):
        for raw in values:
            if not isinstance(raw, dict):
                continue
            participants = raw.get("participants")
            if isinstance(participants, str):
                participants = [participants]
            if not isinstance(participants, list):
                continue
            item = deepcopy(raw)
            item.setdefault("turn", turn_number)
            for cid in dict.fromkeys(str(value) for value in participants if value):
                _memory_bucket(result, cid)["dialogue_memory"].append(deepcopy(item))
    return result


def _refresh_runtime_presence(state: Dict[str, Any], cards: List[Dict[str, Any]], turn_number: int) -> Dict[str, Any]:
    result = deepcopy(state)
    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    result["current"] = current
    present = _present_character_ids(result)
    character_state = result.get("characters") if isinstance(result.get("characters"), dict) else {}
    result["characters"] = character_state
    location = current.get("location")
    for cid in present:
        row = character_state.get(cid) if isinstance(character_state.get(cid), dict) else {}
        row = deepcopy(row)
        row["present"] = True
        if location not in (None, ""):
            row["location"] = location
        row["last_present_turn"] = turn_number
        character_state[cid] = row
    present_set = set(present)
    for cid, row in list(character_state.items()):
        if isinstance(row, dict) and cid not in present_set:
            row = deepcopy(row)
            row["present"] = False
            character_state[cid] = row
    return result


def _initial_state(novel: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = deepcopy(novel.get("starting_state") if isinstance(novel.get("starting_state"), dict) else {})
    if not isinstance(state.get("current"), dict):
        state["current"] = {}
    if not isinstance(state.get("pov"), dict):
        state["pov"] = {}
    if not state["pov"].get("character_id"):
        pov = next((card for card in cards if card.get("is_pov") is True), None)
        if pov is None:
            preferred = novel.get("novel") if isinstance(novel.get("novel"), dict) else {}
            preferred_id = preferred.get("pov_character") or preferred.get("pov_character_id")
            pov = next((card for card in cards if _card_id(card) == str(preferred_id)), None)
        if pov is None and cards:
            pov = cards[0]
        if pov is not None:
            state["pov"]["character_id"] = _card_id(pov)
    if not _present_character_ids(state) and state["pov"].get("character_id"):
        state["current"]["present_characters"] = [state["pov"]["character_id"]]
    return _refresh_runtime_presence(state, cards, 0)


def create_session(novel: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    session_id = uuid.uuid4().hex
    root = SESSIONS_DIR / session_id
    root.mkdir(parents=True, exist_ok=False)
    source = deepcopy(novel)
    cards = _source_characters(source)
    state = _initial_state(source, cards)
    memory = {"characters": {}}
    for card in cards:
        cid = _card_id(card)
        if cid:
            _memory_bucket(memory, cid)
    meta = _template("session_meta.json", {})
    meta.update(
        {
            "session_id": session_id,
            "novel_id": source.get("novel_id") or source.get("id") or "novel",
            "title": source.get("title") or source.get("name") or "Novel",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "turn_number": 0,
            "last_audit_turn": 0,
            "audit_required": False,
            "handoff_required": False,
        }
    )
    _write_json(root / "source.json", source)
    _write_json(root / "characters.json", cards)
    _write_json(root / "state.json", state)
    _write_json(root / "memory.json", memory)
    _write_json(root / "chronology.json", [])
    _write_json(root / "meta.json", meta)
    (root / "turns.jsonl").write_text("", encoding="utf-8")
    return {"session_id": session_id, "turn_number": 0}


def load_session(session_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return {
        "meta": _read_json(root / "meta.json", {}),
        "source": _read_json(root / "source.json", {}),
        "characters": _read_json(root / "characters.json", []),
        "state": _read_json(root / "state.json", {}),
        "memory": _read_json(root / "memory.json", {}),
        "chronology": _read_json(root / "chronology.json", []),
        "turns": _read_turns(root),
    }


def list_novels() -> List[Dict[str, Any]]:
    ensure_dirs()
    result = []
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        try:
            item = _read_json(path, {})
        except Exception:
            continue
        if isinstance(item, dict):
            result.append({"novel_id": item.get("novel_id") or path.stem, "title": item.get("title") or path.stem})
    return result


def save_novel(novel: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    novel_id = str(novel.get("novel_id") or novel.get("id") or uuid.uuid4().hex)
    item = deepcopy(novel)
    item["novel_id"] = novel_id
    _write_json(LIBRARY_DIR / f"{novel_id}.json", item)
    return {"ok": True, "novel_id": novel_id}


def get_novel(novel_id: str) -> Dict[str, Any]:
    path = LIBRARY_DIR / f"{novel_id}.json"
    if not path.exists():
        raise FileNotFoundError(novel_id)
    return _read_json(path, {})


def get_turn_range(session_id: str, start_turn: int, end_turn: int) -> List[Dict[str, Any]]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return [turn for turn in _read_turns(root) if start_turn <= int(turn.get("turn_number", 0)) <= end_turn]


def get_character_memory(session_id: str, character_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    return deepcopy(_memory_bucket(memory, character_id))


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        raise RuntimeError("AUDIT_REQUIRED")
    if meta.get("handoff_required"):
        raise RuntimeError("HANDOFF_REQUIRED")
    turn_number = int(meta.get("turn_number", 0)) + 1
    source = _read_json(root / "source.json", {})
    state = _read_json(root / "state.json", {})
    cards = _load_cards(root, source)
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    chronology = _read_json(root / "chronology.json", [])
    context = {
        "session": {"session_id": session_id, "turn_number": turn_number},
        "user_input": user_input,
        "source": source,
        "state": state,
        "characters": cards,
        "memory": memory,
        "chronology": chronology,
        "recent_turns": _read_turns(root)[-6:],
    }
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + MAX_PACKET_CHARS] for i in range(0, len(text), MAX_PACKET_CHARS)] or ["{}"]
    packet = {
        "packet_id": secrets.token_urlsafe(12),
        "prepared_for_turn": turn_number,
        "user_input": user_input,
        "chunk_count": len(chunks),
        "read_chunks": [],
        "chunks": chunks,
    }
    _write_json(root / "turn_packet.json", packet)
    return {
        "packet_id": packet["packet_id"],
        "prepared_for_turn": turn_number,
        "chunk_count": len(chunks),
        "instruction": "Read every packet chunk before writing or committing the turn.",
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

    extracted = payload.get("extracted", {}) if isinstance(payload.get("extracted"), dict) else {}
    entry = _template("turn.json", {})
    entry.update(
        {
            "turn_number": turn_number,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "user_input": payload["user_input"],
            "scene_output": payload["scene_output"],
            "extracted": deepcopy(extracted),
        }
    )
    with (root / "turns.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

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
        cid = _card_id(card)
        if cid:
            _memory_bucket(memory, cid)
    memory = _apply_memory_events(memory, extracted, turn_number)
    _write_json(root / "memory.json", memory)

    chronology = _read_json(root / "chronology.json", [])
    if not isinstance(chronology, list):
        chronology = []
    if isinstance(extracted.get("chronology"), list):
        chronology.extend(deepcopy(extracted["chronology"]))
    _write_json(root / "chronology.json", chronology)

    meta["turn_number"] = turn_number
    audit_due = turn_number % 15 == 0
    meta["audit_required"] = bool(audit_due)
    handoff_due = turn_number % 60 == 0
    if handoff_due:
        meta["handoff_required"] = True
        _write_json(root / "handoff_tail.json", _read_turns(root)[-6:])
    _write_json(root / "meta.json", meta)
    (root / "turn_packet.json").unlink(missing_ok=True)
    return {
        "ok": True,
        "turn_number": turn_number,
        "audit_due": audit_due,
        "audit_range": [max(1, turn_number - 14), turn_number] if audit_due else None,
        "handoff_required": handoff_due,
    }


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    end_turn = int(meta.get("turn_number", 0))
    start_turn = max(1, int(meta.get("last_audit_turn", 0)) + 1)
    return {
        "session_id": session_id,
        "audit_range": [start_turn, end_turn],
        "turns": get_turn_range(session_id, start_turn, end_turn),
        "state": _read_json(root / "state.json", {}),
        "memory": _read_json(root / "memory.json", {}),
        "chronology": _read_json(root / "chronology.json", []),
    }


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = _read_json(root / "meta.json", {})
    if not meta.get("audit_required"):
        raise RuntimeError("AUDIT_NOT_REQUIRED")
    expected_end = int(meta.get("turn_number", 0))
    if int(payload.get("end_turn", 0)) != expected_end:
        raise ValueError("AUDIT_RANGE_MISMATCH")

    repairs = payload.get("repairs", {}) if isinstance(payload.get("repairs"), dict) else {}
    if isinstance(repairs.get("state_patch"), dict):
        state = _deep_merge(_read_json(root / "state.json", {}), repairs["state_patch"])
        _write_json(root / "state.json", state)
    memory = _normalise_memory(_read_json(root / "memory.json", {}))
    memory = _apply_memory_events(memory, repairs, expected_end)
    _write_json(root / "memory.json", memory)
    chronology = _read_json(root / "chronology.json", [])
    if isinstance(repairs.get("chronology_add"), list):
        chronology.extend(deepcopy(repairs["chronology_add"]))
    _write_json(root / "chronology.json", chronology)

    audits = _read_json(root / "audits.json", [])
    if not isinstance(audits, list):
        audits = []
    audits.append(
        {
            "start_turn": payload["start_turn"],
            "end_turn": payload["end_turn"],
            "repairs": deepcopy(repairs),
            "notes": deepcopy(payload.get("notes", [])),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _write_json(root / "audits.json", audits)
    meta["last_audit_turn"] = payload["end_turn"]
    meta["audit_required"] = False
    _write_json(root / "meta.json", meta)
    return {"ok": True, "audited_through": payload["end_turn"], "handoff_required": bool(meta.get("handoff_required"))}
