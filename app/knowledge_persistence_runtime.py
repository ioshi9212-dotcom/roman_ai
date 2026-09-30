from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List

from . import storage


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _resolve_character_id(cards: List[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    needle = _norm(value)
    if not needle:
        return None
    for card in cards:
        cid = storage._card_id(card)
        if _norm(cid) == needle:
            return cid
        if any(_norm(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _explicit_participants(cards: List[Dict[str, Any]], raw: Dict[str, Any]) -> List[str]:
    values = raw.get("knowledge_participants")
    if isinstance(values, (str, dict)):
        values = [values]
    if not isinstance(values, list):
        return []
    result: List[str] = []
    for value in values:
        cid = _resolve_character_id(cards, value)
        if cid and cid not in result:
            result.append(cid)
    return result


def _event_text(event: Dict[str, Any]) -> str:
    return " ".join(
        str(
            event.get("event")
            or event.get("summary")
            or event.get("fact")
            or event.get("description")
            or ""
        ).split()
    ).strip()


def _journal_contains(rows: Iterable[Dict[str, Any]], character_id: str, text: str) -> bool:
    needle = _norm(text)
    if not needle:
        return False
    for row in rows:
        if not isinstance(row, dict) or str(row.get("character_id") or "") != character_id:
            continue
        existing = _norm(row.get("text") or row.get("fact") or row.get("summary") or "")
        if existing == needle or (len(needle) >= 24 and needle in existing):
            return True
    return False


def attach_explicit_chronology_participants(
    session_id: str,
    payload: Dict[str, Any],
    raw_chronology: Any,
) -> Dict[str, Any]:
    """Annotate normalized chronology with only explicitly supplied knowledge participants.

    Chronology participants describe scene involvement and may be inferred from physical presence. Only the dedicated knowledge_participants field is safe to grant personal knowledge.
    """
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    chronology = extracted.get("chronology")
    raw_rows = raw_chronology if isinstance(raw_chronology, list) else []
    if not isinstance(chronology, list) or not chronology:
        return result

    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)

    annotated: List[Dict[str, Any]] = []
    for index, event in enumerate(chronology):
        if not isinstance(event, dict):
            continue
        item = deepcopy(event)
        raw = raw_rows[index] if index < len(raw_rows) and isinstance(raw_rows[index], dict) else {}
        participants = _explicit_participants(cards, raw)
        if participants:
            item["knowledge_participants"] = participants
        annotated.append(item)

    extracted["chronology"] = annotated
    result["extracted"] = extracted
    return result


def mirror_explicit_chronology_to_personal_memory(
    session_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Mirror durable chronology meaning into participants' personal factual memory.

    This only uses knowledge_participants created from an explicit model-supplied
    participants list, never inferred scene presence.
    """
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    chronology = extracted.get("chronology")
    if not isinstance(chronology, list):
        return result

    journal = extracted.get("knowledge_journal_add")
    journal = deepcopy(journal) if isinstance(journal, list) else []

    for event in chronology:
        if not isinstance(event, dict):
            continue
        text = _event_text(event)
        participants = event.get("knowledge_participants")
        if not text or not isinstance(participants, list):
            continue
        for character_id in participants:
            cid = str(character_id or "").strip()
            if not cid or _journal_contains(journal, cid, text):
                continue
            journal.append({
                "character_id": cid,
                "date": event.get("story_date") or event.get("date"),
                "period": event.get("period"),
                "text": text,
            })

    extracted["knowledge_journal_add"] = journal
    result["extracted"] = extracted
    return result


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


def repair_personal_memory_from_safe_chronology(session_id: str) -> int:
    """Repair missed journal writes from chronology carrying explicit knowledge participants.

    Safe to run before every packet. It does not infer knowledge from ordinary chronology
    rows created before this contract.
    """
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    chronology = storage._read_json(root / "chronology.json", [])
    if not isinstance(chronology, list) or not chronology:
        return 0

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    added = 0
    counters: Dict[tuple[str, int], int] = {}

    for event in chronology:
        if not isinstance(event, dict):
            continue
        participants = event.get("knowledge_participants")
        text = _event_text(event)
        if not isinstance(participants, list) or not text:
            continue
        turn = int(event.get("turn_number", 0) or 0)
        for character_id in participants:
            cid = str(character_id or "").strip()
            if not cid:
                continue
            bucket = storage._memory_bucket(memory, cid)
            existing = bucket.get("knowledge_journal", [])
            if isinstance(existing, list) and any(
                _norm(row.get("text") if isinstance(row, dict) else "") == _norm(text)
                for row in existing
            ):
                continue
            key = (cid, turn)
            counters[key] = counters.get(key, 0) + 1
            bucket["knowledge_journal"].append({
                "entry_id": f"chrono_repair_t{turn}_{counters[key]}",
                "date": event.get("story_date") or event.get("date"),
                "period": event.get("period"),
                "text": text,
                "turn": turn,
            })
            added += 1

    if added:
        storage._write_json(root / "memory.json", memory)
    return added
