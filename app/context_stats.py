from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from . import storage


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def session_context_stats(session_id: str) -> Dict[str, Any]:
    """Return size/count diagnostics only. Never writes session data."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    source = storage._read_json(root / "source.json", {})
    state = storage._read_json(root / "state.json", {})
    cards = storage._read_json(root / "characters.json", [])
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    meta = storage._read_json(root / "meta.json", {})

    memory_rows: Dict[str, Any] = {}
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    for character_id, bucket in characters.items():
        if not isinstance(bucket, dict):
            continue
        memory_rows[str(character_id)] = {
            "chars": _json_chars(bucket),
            "knowledge": _list_count(bucket.get("knowledge")),
            "experiences": _list_count(bucket.get("experiences")),
            "dialogue_memory": _list_count(bucket.get("dialogue_memory")),
        }

    memory_rows = dict(
        sorted(memory_rows.items(), key=lambda item: item[1]["chars"], reverse=True)
    )

    chronology_chars = [_json_chars(item) for item in chronology] if isinstance(chronology, list) else []
    chronology_importance: Dict[str, int] = {}
    if isinstance(chronology, list):
        for item in chronology:
            if not isinstance(item, dict):
                continue
            importance = str(item.get("importance") or "unspecified")
            chronology_importance[importance] = chronology_importance.get(importance, 0) + 1

    files = {}
    for name in (
        "source.json",
        "state.json",
        "characters.json",
        "memory.json",
        "chronology.json",
        "turns.jsonl",
        "audits.json",
        "meta.json",
    ):
        files[name] = _file_bytes(root / name)

    total_known_bytes = sum(files.values())
    return {
        "session_id": session_id,
        "turn_number": int(meta.get("turn_number") or 0),
        "read_only": True,
        "files_bytes": files,
        "known_session_bytes": total_known_bytes,
        "json_chars": {
            "source": _json_chars(source),
            "state": _json_chars(state),
            "characters": _json_chars(cards),
            "memory": _json_chars(memory),
            "chronology": _json_chars(chronology),
        },
        "chronology": {
            "events": len(chronology) if isinstance(chronology, list) else 0,
            "chars": _json_chars(chronology),
            "average_event_chars": round(sum(chronology_chars) / len(chronology_chars), 1) if chronology_chars else 0,
            "largest_event_chars": max(chronology_chars, default=0),
            "importance_counts": chronology_importance,
        },
        "memory": {
            "characters": len(memory_rows),
            "chars": _json_chars(memory),
            "by_character": memory_rows,
        },
    }
