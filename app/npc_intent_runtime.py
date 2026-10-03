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


_INTENT_KNOWLEDGE_FIELDS = (
    "summary",
    "trigger",
    "why_it_matters",
    "planned_action",
    "last_outcome",
    "resolution",
)


def _intent_text(raw: Dict[str, Any]) -> str:
    return "\n".join(
        str(raw.get(field) or "").strip()
        for field in _INTENT_KNOWLEDGE_FIELDS
        if str(raw.get(field) or "").strip()
    )


def _same_commit_personal_text(container: Dict[str, Any], character_id: str) -> str:
    pieces: list[str] = []
    for field in ("knowledge_journal_add", "knowledge_add", "dialogue_memory_add"):
        values = container.get(field)
        if not isinstance(values, list):
            continue
        for row in values:
            if not isinstance(row, dict) or str(row.get("character_id") or row.get("owner_character_id") or "") != character_id:
                continue
            for key in ("text", "content", "fact", "summary", "last_outcome"):
                value = str(row.get(key) or "").strip()
                if value:
                    pieces.append(value)
            segments = row.get("segments")
            if isinstance(segments, list):
                for segment in segments:
                    if isinstance(segment, dict):
                        value = str(segment.get("text") or "").strip()
                        if value:
                            pieces.append(value)
    return "\n".join(pieces)


def _other_personal_knowledge_rows(root, character_id: str) -> list[tuple[str, str]]:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    rows: list[tuple[str, str]] = []
    for source_id, bucket in characters.items():
        source_id = str(source_id)
        if source_id == character_id or not isinstance(bucket, dict):
            continue
        journal = bucket.get("knowledge_journal")
        if isinstance(journal, list):
            for row in journal:
                if not isinstance(row, dict):
                    continue
                value = str(row.get("text") or row.get("fact") or row.get("summary") or "").strip()
                if value:
                    rows.append((source_id, value))
        knowledge = bucket.get("knowledge")
        if isinstance(knowledge, list):
            for row in knowledge:
                if isinstance(row, dict):
                    value = str(row.get("content") or row.get("text") or row.get("fact") or row.get("summary") or "").strip()
                else:
                    value = str(row or "").strip()
                if value:
                    rows.append((source_id, value))
        dialogue = bucket.get("dialogue_memory")
        if isinstance(dialogue, list):
            for row in dialogue:
                if not isinstance(row, dict):
                    continue
                value = str(row.get("summary") or "").strip()
                if value:
                    rows.append((source_id, value))
                segments = row.get("segments")
                if isinstance(segments, list):
                    for segment in segments:
                        if isinstance(segment, dict):
                            value = str(segment.get("text") or "").strip()
                            if value:
                                rows.append((source_id, value))
    return rows


def _validate_intent_personal_knowledge(root, container: Dict[str, Any], updates: Any) -> None:
    if not isinstance(updates, list):
        return

    # Local import avoids coupling the intent storage module to the scene validator
    # during module initialization while reusing the same conservative term matcher.
    from . import private_knowledge_runtime

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    allowed_cache: Dict[str, set[str]] = {}
    protected_cache: Dict[str, list[tuple[str, str]]] = {}

    for raw in updates:
        if not isinstance(raw, dict):
            continue
        character_id = str(raw.get("character_id") or "")
        text = _intent_text(raw)
        if not character_id or not text:
            continue

        if character_id not in allowed_cache:
            allowed_text = private_knowledge_runtime._authorized_corpus(root, character_id, cards)
            same_commit = _same_commit_personal_text(container, character_id)
            allowed_cache[character_id] = private_knowledge_runtime._terms(
                allowed_text + "\n" + same_commit
            )
        if character_id not in protected_cache:
            protected_cache[character_id] = _other_personal_knowledge_rows(root, character_id)

        allowed_terms = allowed_cache[character_id]
        for source_character_id, protected_text in protected_cache[character_id]:
            protected_terms = private_knowledge_runtime._terms(protected_text)
            if not protected_terms:
                continue
            leaked = private_knowledge_runtime._leaked_terms(
                text,
                protected_terms,
                allowed_terms,
                protected_payload=protected_text,
            )
            if leaked:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "NPC_INTENT_PERSONAL_KNOWLEDGE_LEAK",
                        "message": (
                            "An NPC intent contains concrete detail present in another character's personal knowledge "
                            "but absent from the intent owner's own knowledge. Keep the intent at the unresolved goal/question level "
                            "until this NPC actually learns the detail."
                        ),
                        "character_id": character_id,
                        "source_character_id": source_character_id,
                        "leaked_terms": leaked,
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

    # source_fact_ids are optional in the current simple knowledge journal, so IDs
    # alone cannot protect intent state. Also reject textual laundering of another
    # character's personal knowledge into summary/trigger/planned_action/etc.
    _validate_intent_personal_knowledge(root, container, updates)


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
