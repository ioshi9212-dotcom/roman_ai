from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import session_runtime, storage


_ORIGINAL_COMMIT_TURN = None
_ORIGINAL_COMMIT_AUDIT = None


def _error(code: str, message: str) -> None:
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _resolve(cards: Iterable[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name") or value.get("full_name")
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


def _canonical_owner(cards: List[Dict[str, Any]], item: Dict[str, Any], field: str) -> Dict[str, Any]:
    result = deepcopy(item)
    raw = result.get("character_id") or result.get("owner_character_id")
    if raw in (None, ""):
        _error(
            "MEMORY_CHARACTER_REQUIRED",
            f"{field} record has no character_id. Refusing to silently drop durable memory.",
        )
    resolved = _resolve(cards, raw)
    if not resolved:
        _error(
            "MEMORY_CHARACTER_UNKNOWN",
            f"{field} references an unknown character. Use a registered character_id or a resolvable saved alias.",
        )
    result["character_id"] = resolved
    result.pop("owner_character_id", None)
    return result


def _as_values(value: Any) -> List[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _canonical_dialogue(cards: List[Dict[str, Any]], item: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(item)
    raw_values: List[Any] = []
    for key in ("participants", "participant_ids"):
        raw_values.extend(_as_values(result.get(key)))
    for key in ("character_id", "asked_by", "asked_to", "speaker", "listener", "said_by", "heard_by"):
        raw_values.extend(_as_values(result.get(key)))

    participants: List[str] = []
    unresolved: List[str] = []
    for value in raw_values:
        resolved = _resolve(cards, value)
        if resolved:
            participants.append(resolved)
        elif value not in (None, ""):
            unresolved.append(str(value))

    participants = list(dict.fromkeys(participants))
    if not participants:
        _error(
            "DIALOGUE_MEMORY_PARTICIPANTS_REQUIRED",
            "dialogue_memory_add has no resolvable participants. Refusing to report a successful commit while dropping the dialogue memory.",
        )

    result["participants"] = participants
    result.pop("participant_ids", None)
    if result.get("character_id") not in (None, ""):
        resolved = _resolve(cards, result.get("character_id"))
        if resolved:
            result["character_id"] = resolved
    for key in ("asked_by", "asked_to", "speaker", "listener", "said_by", "heard_by"):
        if result.get(key) in (None, ""):
            continue
        resolved = _resolve(cards, result.get(key))
        if resolved:
            result[key] = resolved

    if unresolved:
        result["unresolved_participant_labels"] = list(dict.fromkeys(unresolved))[:8]
    return result


def _canonicalize_memory_payload(session_id: str, payload: Dict[str, Any], *, audit: bool) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    result = deepcopy(payload)
    container_key = "repairs" if audit else "extracted"
    container = result.get(container_key)
    if not isinstance(container, dict):
        return result

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    if not audit:
        cards = storage._apply_character_upserts(cards, container)

    for field in ("knowledge_add", "experiences_add"):
        values = container.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            continue
        container[field] = [
            _canonical_owner(cards, item, field)
            for item in values
            if isinstance(item, dict)
        ]

    dialogue = container.get("dialogue_memory_add")
    if isinstance(dialogue, list):
        container["dialogue_memory_add"] = [
            _canonical_dialogue(cards, item)
            for item in dialogue
            if isinstance(item, dict)
        ]

    result[container_key] = container
    return result


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = _canonicalize_memory_payload(session_id, payload, audit=False)
    return _ORIGINAL_COMMIT_TURN(session_id, prepared)


def _commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = _canonicalize_memory_payload(session_id, payload, audit=True)
    return _ORIGINAL_COMMIT_AUDIT(session_id, prepared)


def install() -> None:
    global _ORIGINAL_COMMIT_TURN, _ORIGINAL_COMMIT_AUDIT
    _ORIGINAL_COMMIT_TURN = session_runtime.commit_turn
    _ORIGINAL_COMMIT_AUDIT = session_runtime.commit_audit
    session_runtime.commit_turn = _commit_turn
    session_runtime.commit_audit = _commit_audit
