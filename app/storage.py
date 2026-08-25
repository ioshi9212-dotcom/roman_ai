import json
import os
import uuid
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
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_novels() -> List[Dict[str, Any]]:
    ensure_dirs()
    result = []
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        data = _read_json(path, {})
        result.append({
            "novel_id": data.get("novel_id"),
            "title": data.get("title"),
            "version": data.get("version", 1),
        })
    return result


def save_novel(template: Dict[str, Any]) -> Dict[str, Any]:
    ensure_dirs()
    path = LIBRARY_DIR / f"{template['novel_id']}.json"
    _write_json(path, template)
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
    }
    _write_json(root / "meta.json", meta)
    _write_json(root / "source.json", novel)
    _write_json(root / "state.json", {})
    _write_json(root / "chronology.json", [])
    (root / "turns.jsonl").write_text("", encoding="utf-8")
    return meta


def load_session(session_id: str, recent_limit: int = 6) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    turns = []
    turns_path = root / "turns.jsonl"
    if turns_path.exists():
        for line in turns_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                turns.append(json.loads(line))

    return {
        "meta": _read_json(root / "meta.json", {}),
        "source": _read_json(root / "source.json", {}),
        "state": _read_json(root / "state.json", {}),
        "chronology": _read_json(root / "chronology.json", []),
        "recent_turns": turns[-recent_limit:],
    }


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    meta = _read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0)) + 1

    entry = {
        "turn_number": turn_number,
        "user_input": payload["user_input"],
        "scene_output": payload["scene_output"],
        "extracted": payload.get("extracted", {}),
    }

    with (root / "turns.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    extracted = payload.get("extracted", {})
    state = _read_json(root / "state.json", {})
    if isinstance(extracted.get("state_patch"), dict):
        state.update(extracted["state_patch"])
        _write_json(root / "state.json", state)

    chronology = _read_json(root / "chronology.json", [])
    if isinstance(extracted.get("chronology"), list):
        chronology.extend(extracted["chronology"])
        _write_json(root / "chronology.json", chronology)

    meta["turn_number"] = turn_number
    _write_json(root / "meta.json", meta)

    return {"ok": True, "turn_number": turn_number}
