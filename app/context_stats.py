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


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def session_context_stats(session_id: str) -> Dict[str, Any]:
    """Read-only diagnostics. This function never writes session data."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    source = storage._read_json(root / "source.json", {})
    state = storage._read_json(root / "state.json", {})
    cards = storage._read_json(root / "characters.json", [])
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    meta = storage._read_json(root / "meta.json", {})

    by_character: Dict[str, Any] = {}
    memory_characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    for character_id, bucket in memory_characters.items():
        if not isinstance(bucket, dict):
            continue
        by_character[str(character_id)] = {
            "chars": _json_chars(bucket),
            "knowledge": _count(bucket.get("knowledge")),
            "experiences": _count(bucket.get("experiences")),
            "dialogue_memory": _count(bucket.get("dialogue_memory")),
        }
    by_character = dict(sorted(by_character.items(), key=lambda item: item[1]["chars"], reverse=True))

    chronology_event_chars = []
    importance_counts: Dict[str, int] = {}
    if isinstance(chronology, list):
        for event in chronology:
            chronology_event_chars.append(_json_chars(event))
            if isinstance(event, dict):
                importance = str(event.get("importance") or "unspecified")
                importance_counts[importance] = importance_counts.get(importance, 0) + 1

    files = {
        name: _file_bytes(root / name)
        for name in (
            "source.json",
            "state.json",
            "characters.json",
            "memory.json",
            "chronology.json",
            "turns.jsonl",
            "audits.json",
            "meta.json",
            "turn_packet.json",
            "audit_packet.json",
        )
    }

    return {
        "session_id": session_id,
        "turn_number": int(meta.get("turn_number") or 0),
        "read_only": True,
        "known_session_bytes": sum(files.values()),
        "files_bytes": files,
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
            "average_event_chars": round(sum(chronology_event_chars) / len(chronology_event_chars), 1) if chronology_event_chars else 0,
            "largest_event_chars": max(chronology_event_chars, default=0),
            "importance_counts": importance_counts,
        },
        "memory": {
            "characters": len(by_character),
            "chars": _json_chars(memory),
            "by_character": by_character,
        },
    }
