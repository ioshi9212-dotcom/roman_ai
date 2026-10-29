from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _journal_event_key(row: Dict[str, Any]) -> tuple[str, ...]:
    """Same words on a different day, turn or from a different source aren't duplicates."""
    return (
        _norm(row.get("text") or row.get("fact") or row.get("summary")),
        _norm(row.get("date")),
        _norm(row.get("period")),
        _norm(row.get("turn") or row.get("learned_turn") or row.get("source_turn")),
        _norm(row.get("speaker_id") or row.get("source_character_id") or row.get("source")),
        _norm(row.get("source_type") or row.get("channel")),
    )


def dedupe_persisted_knowledge_journal(session_id: str) -> int:
    """Remove only exact normalized duplicate durable facts already stored for one character."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    removed = 0

    for bucket in characters.values():
        if not isinstance(bucket, dict):
            continue
        rows = bucket.get("knowledge_journal")
        if not isinstance(rows, list) or len(rows) < 2:
            continue
        seen: set[tuple[str, ...]] = set()
        clean: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _journal_event_key(row)
            if not key[0]:
                clean.append(deepcopy(row))
                continue
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            clean.append(deepcopy(row))
        bucket["knowledge_journal"] = clean

    if removed:
        storage._write_json(root / "memory.json", memory)
    return removed


def dedupe_new_journal_against_persisted(
    session_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Drop exact duplicate journal facts before storage assigns a fresh entry_id."""
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    rows = extracted.get("knowledge_journal_add")
    if not isinstance(rows, list) or not rows:
        return result

    root = storage.SESSIONS_DIR / session_id
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    known: Dict[str, set[tuple[str, ...]]] = {}

    def events_for(character_id: str) -> set[tuple[str, ...]]:
        if character_id in known:
            return known[character_id]
        bucket = characters.get(character_id) if isinstance(characters.get(character_id), dict) else {}
        existing = bucket.get("knowledge_journal") if isinstance(bucket, dict) else []
        values = {
            _journal_event_key(row)
            for row in existing
            if isinstance(row, dict)
        } if isinstance(existing, list) else set()
        known[character_id] = values
        return values

    clean: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or "").strip()
        key = _journal_event_key(row)
        if not cid or not key[0]:
            clean.append(deepcopy(row))
            continue
        seen = events_for(cid)
        if key in seen:
            continue
        seen.add(key)
        clean.append(deepcopy(row))

    extracted["knowledge_journal_add"] = clean
    result["extracted"] = extracted
    return result
