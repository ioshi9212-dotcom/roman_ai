from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable

from fastapi import HTTPException

from . import session_runtime, storage
from .npc_intent import apply_updates


_ORIGINAL_COMMIT_TURN = None
_ORIGINAL_COMMIT_AUDIT = None


def _persistent_fact_ids(root, character_id: str) -> set[str]:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = memory.get("characters", {}).get(character_id, {}) if isinstance(memory.get("characters"), dict) else {}
    result: set[str] = set()
    for item in bucket.get("knowledge", []) if isinstance(bucket, dict) and isinstance(bucket.get("knowledge"), list) else []:
        if isinstance(item, dict) and item.get("fact_id"):
            result.add(str(item["fact_id"]))
    return result


def _same_commit_fact_ids(container: Dict[str, Any], character_id: str) -> set[str]:
    result: set[str] = set()
    values = container.get("knowledge_add") if isinstance(container.get("knowledge_add"), list) else []
    for item in values:
        if not isinstance(item, dict):
            continue
        if str(item.get("character_id") or "") != character_id:
            continue
        if item.get("fact_id"):
            result.add(str(item["fact_id"]))
    return result


def _source_ids(raw: Dict[str, Any]) -> Iterable[str]:
    values = raw.get("source_fact_ids")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value not in (None, "")]


def _source_error(character_id: str, unknown: list[str]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "NPC_INTENT_SOURCE_FACT_UNKNOWN",
            "message": (
                "An NPC intent cited a fact that is not in this character's persisted personal knowledge "
                "and is not being added to this same character in the current commit. Do not create future behavior from author-only knowledge."
            ),
            "character_id": character_id,
            "unknown_source_fact_ids": unknown,
        },
    )


def _validate_intent_sources(root, container: Dict[str, Any], updates: Any) -> None:
    if not isinstance(updates, list):
        return
    known_cache: Dict[str, set[str]] = {}
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        source_ids = list(_source_ids(raw))
        if not source_ids:
            continue
        character_id = str(raw.get("character_id") or "")
        if not character_id:
            _source_error(character_id, source_ids)
        if character_id not in known_cache:
            known_cache[character_id] = _persistent_fact_ids(root, character_id) | _same_commit_fact_ids(container, character_id)
        unknown = [source_id for source_id in source_ids if source_id not in known_cache[character_id]]
        if unknown:
            _source_error(character_id, unknown)


def _with_intent_patch(session_id: str, payload: Dict[str, Any], *, audit: bool = False) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0) + (0 if audit else 1)

    container_key = "repairs" if audit else "extracted"
    container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
    updates = container.get("npc_intent_updates")
    if not isinstance(updates, list) or not updates:
        return result

    # An intent may cite only facts this exact NPC already knows, or facts added
    # to that NPC's personal memory in the same transactional commit. This keeps
    # future autonomous behavior from laundering author knowledge into character knowledge.
    _validate_intent_sources(root, container, updates)

    updated_state = apply_updates(state, updates, current_turn=current_turn)
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    patch = deepcopy(patch)
    patch["npc_intents"] = deepcopy(updated_state.get("npc_intents", {}))
    container = deepcopy(container)
    container["state_patch"] = patch
    result[container_key] = container
    return result


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_TURN(session_id, _with_intent_patch(session_id, payload, audit=False))


def _commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_AUDIT(session_id, _with_intent_patch(session_id, payload, audit=True))


def install() -> None:
    global _ORIGINAL_COMMIT_TURN, _ORIGINAL_COMMIT_AUDIT
    if _ORIGINAL_COMMIT_TURN is not None:
        return
    _ORIGINAL_COMMIT_TURN = session_runtime.commit_turn
    _ORIGINAL_COMMIT_AUDIT = session_runtime.commit_audit
    session_runtime.commit_turn = _commit_turn
    session_runtime.commit_audit = _commit_audit
