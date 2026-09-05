from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from . import storage


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_chars(value: Any) -> int:
    return len(_json_text(value))


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _sorted_sizes(mapping: Dict[str, Any]) -> Dict[str, int]:
    rows = {str(key): _json_chars(value) for key, value in mapping.items()}
    return dict(sorted(rows.items(), key=lambda item: item[1], reverse=True))


def _packet_breakdown(root: Path) -> Dict[str, Any]:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet:
        return {"present": False}

    chunks = packet.get("chunks") if isinstance(packet.get("chunks"), list) else []
    raw = "".join(str(part) for part in chunks)
    payload: Any = None
    parse_error = None
    if raw:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            parse_error = str(exc)

    result: Dict[str, Any] = {
        "present": True,
        "file_chars": _json_chars(packet),
        "chunk_count": len(chunks),
        "payload_chars": len(raw),
        "packet_metadata_chars": _json_chars({key: value for key, value in packet.items() if key != "chunks"}),
    }
    if parse_error:
        result["parse_error"] = parse_error
        return result
    if not isinstance(payload, dict):
        result["payload_type"] = type(payload).__name__
        return result

    result["top_level_chars"] = _sorted_sizes(payload)
    nested: Dict[str, Dict[str, int]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            nested[str(key)] = _sorted_sizes(value)
    result["nested_dict_chars"] = nested

    fingerprints: Dict[str, list] = {}
    serialized_sizes: Dict[str, int] = {}

    def remember(path: str, value: Any) -> None:
        text = _json_text(value)
        if len(text) < 500:
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        fingerprints.setdefault(digest, []).append(path)
        serialized_sizes[digest] = len(text)

    for key, value in payload.items():
        remember(str(key), value)
        if isinstance(value, dict):
            for subkey, subvalue in value.items():
                remember(f"{key}.{subkey}", subvalue)

    duplicates = [
        {"chars": serialized_sizes[digest], "paths": paths}
        for digest, paths in fingerprints.items()
        if len(paths) > 1
    ]
    duplicates.sort(key=lambda row: row["chars"], reverse=True)
    result["exact_duplicate_blocks"] = duplicates
    return result


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
        "turn_packet": _packet_breakdown(root),
    }
