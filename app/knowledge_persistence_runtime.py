from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


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
        seen: set[str] = set()
        clean: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            text = _norm(row.get("text") or row.get("fact") or row.get("summary") or "")
            if not text:
                clean.append(deepcopy(row))
                continue
            if text in seen:
                removed += 1
                continue
            seen.add(text)
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
    known: Dict[str, set[str]] = {}

    def texts_for(character_id: str) -> set[str]:
        if character_id in known:
            return known[character_id]
        bucket = characters.get(character_id) if isinstance(characters.get(character_id), dict) else {}
        existing = bucket.get("knowledge_journal") if isinstance(bucket, dict) else []
        values = {
            _norm(row.get("text") or row.get("fact") or row.get("summary") or "")
            for row in existing
            if isinstance(row, dict)
        } if isinstance(existing, list) else set()
        values.discard("")
        known[character_id] = values
        return values

    clean: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or "").strip()
        text = _norm(row.get("text") or row.get("fact") or row.get("summary") or "")
        if not cid or not text:
            clean.append(deepcopy(row))
            continue
        seen = texts_for(cid)
        if text in seen:
            continue
        seen.add(text)
        clean.append(deepcopy(row))

    extracted["knowledge_journal_add"] = clean
    result["extracted"] = extracted
    return result
