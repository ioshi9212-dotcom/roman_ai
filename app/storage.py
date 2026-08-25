import json
import os
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LIBRARY_DIR = DATA_DIR / "library"
SESSIONS_DIR = DATA_DIR / "sessions"


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
    meta = {
        "session_id": session_id,
        "source_novel_id": novel["novel_id"],
        "source_novel_version": novel.get("version", 1),
        "turn_number": 0,
        "audit_required": False,
        "handoff_required": False,
        "handoff_generation": 0,
        "last_audit_turn": 0,
    }
    initial_state = {
        "current": {},
        "characters": {},
        "knowledge": {},
        "relationships": {},
        "threads": {},
        "world": {},
    }
    _write_json(root / "meta.json", meta)
    _write_json(root / "source.json", novel)
    _write_json(root / "state.json", initial_state)
    _write_json(root / "chronology.json", [])
    _write_json(root / "audits.json", [])
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
        "chronology": _read_json(root / "chronology.json", []),
        "recent_turns": turns[-recent_limit:],
    }


def get_turn_range(session_id: str, start_turn: int, end_turn: int) -> List[Dict[str, Any]]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    return [t for t in _read_turns(root) if start_turn <= int(t.get("turn_number", 0)) <= end_turn]


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
    entry = {
        "turn_number": turn_number,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "user_input": payload["user_input"],
        "scene_output": payload["scene_output"],
        "extracted": payload.get("extracted", {}),
    }
    with (root / "turns.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    extracted = payload.get("extracted", {})
    state = _read_json(root / "state.json", {})
    if isinstance(extracted.get("state_patch"), dict):
        state = _deep_merge(state, extracted["state_patch"])
        _write_json(root / "state.json", state)

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
        "chronology": _read_json(root / "chronology.json", []),
        "handoff_tail": _read_json(root / "handoff_tail.json", []),
        "instruction": "Restore this exact session. Treat source as canon, state as current truth, chronology as persistent history, and handoff_tail as the exact recent scene continuity. Do not summarize away details from handoff_tail before continuing.",
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
    for name in ("handoff_tail.json", "resume_token.json"):
        path = root / name
        if path.exists():
            path.unlink()
    return {"ok": True, "session_id": session_id, "turn_number": meta.get("turn_number"), "handoff_generation": meta["handoff_generation"]}
