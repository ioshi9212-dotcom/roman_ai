from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from .character_access import get_character_bundle


CHARACTER_CHUNK_CHARS = 12000
CHARACTER_WORKING_KNOWLEDGE = 18
CHARACTER_WORKING_EXPERIENCES = 12
CHARACTER_WORKING_DIALOGUE = 12
CHARACTER_HISTORICAL_CATALOG = 8
MAX_INTENT_SOURCE_FACTS = 12
_ORIGINAL_INJECT = None


def _turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        return int(item.get("learned_turn") or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("fact_id", "event_id", "topic_id", "id"):
        if item.get(key):
            return str(item[key])
    return None


def _summary(item: Dict[str, Any]) -> str:
    for key in ("fact", "summary", "event", "description", "text", "content", "topic", "memory", "note", "detail"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:220]
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))[:220]


def _tail(values: Any, limit: int) -> List[Dict[str, Any]]:
    rows = [deepcopy(item) for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    rows.sort(key=_turn)
    return rows[-limit:]


def _intent_source_ids(bundle: Dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for intent in bundle.get("active_intents", []) if isinstance(bundle.get("active_intents"), list) else []:
        if not isinstance(intent, dict):
            continue
        values = intent.get("source_fact_ids")
        if not isinstance(values, list):
            continue
        for value in values[:MAX_INTENT_SOURCE_FACTS]:
            if value not in (None, ""):
                result.add(str(value))
    return result


def _working_memory(bundle: Dict[str, Any]) -> Dict[str, Any]:
    memory = bundle.get("personal_memory") if isinstance(bundle.get("personal_memory"), dict) else {}
    all_knowledge = [deepcopy(item) for item in memory.get("knowledge", []) if isinstance(item, dict)] if isinstance(memory.get("knowledge"), list) else []
    recent_knowledge = _tail(all_knowledge, CHARACTER_WORKING_KNOWLEDGE)
    selected_ids = {_id(item) for item in recent_knowledge if _id(item)}

    # If an active intent was born from an old fact, keep that exact known premise
    # beside the intent so autonomous follow-up cannot drift into invented knowledge.
    source_ids = _intent_source_ids(bundle)
    source_rows = [
        deepcopy(item)
        for item in all_knowledge
        if _id(item) in source_ids and _id(item) not in selected_ids
    ][:MAX_INTENT_SOURCE_FACTS]
    knowledge = [*source_rows, *recent_knowledge]

    selected_ids = {_id(item) for item in knowledge if _id(item)}
    older = [item for item in all_knowledge if _id(item) not in selected_ids]
    catalog = [
        {
            "fact_id": _id(item),
            "learned_turn": _turn(item),
            "summary": _summary(item),
        }
        for item in older[-CHARACTER_HISTORICAL_CATALOG:]
    ]

    experiences = _tail(memory.get("experiences"), CHARACTER_WORKING_EXPERIENCES)
    dialogue = _tail(memory.get("dialogue_memory"), CHARACTER_WORKING_DIALOGUE)
    return {
        "knowledge": knowledge,
        "experiences": experiences,
        "dialogue_memory": dialogue,
        "historical_knowledge_catalog": [
            {key: value for key, value in row.items() if value not in (None, "", 0)}
            for row in catalog
        ],
        "persistent_counts": {
            "knowledge": len(all_knowledge),
            "experiences": len(memory.get("experiences", [])) if isinstance(memory.get("experiences"), list) else 0,
            "dialogue_memory": len(memory.get("dialogue_memory", [])) if isinstance(memory.get("dialogue_memory"), list) else 0,
        },
        "older_history_available": True,
    }


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    full = get_character_bundle(session_id, character_id)
    return {
        "character_id": character_id,
        "card": deepcopy(full.get("card", {})),
        "current_state": deepcopy(full.get("current_state", {})),
        "pov_familiarity": deepcopy(full.get("pov_familiarity")),
        "personal_memory": _working_memory(full),
        "relationship_to_pov": deepcopy(full.get("relationship_to_pov")),
        "active_intents": deepcopy(full.get("active_intents", [])),
        "working_bundle": True,
        "persistent_lifetime_memory_complete": True,
        "instruction": (
            "Use this full card plus bounded personal working memory, relationship and active intents to write this character. "
            "Knowledge supporting an active intent is included when source_fact_ids identify it. Complete lifetime memory remains persistent in Railway and is intentionally not retransmitted for every offscreen entrance/message/call. "
            "Do not infer private current-scene facts from author context."
        ),
    }


def _snapshot(session_id: str, character_id: str) -> tuple[str, List[str]]:
    bundle = _participation_bundle(session_id, character_id)
    text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    read_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    chunks = [text[i:i + CHARACTER_CHUNK_CHARS] for i in range(0, len(text), CHARACTER_CHUNK_CHARS)] or ["{}"]
    return read_id, chunks


def prepare_character_bundle_read(session_id: str, character_id: str) -> Dict[str, Any]:
    read_id, chunks = _snapshot(session_id, character_id)
    result: Dict[str, Any] = {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_count": len(chunks),
        "chunk_chars_max": CHARACTER_CHUNK_CHARS,
        "first_chunk_included": bool(chunks),
        "next_chunk_index": 1 if len(chunks) > 1 else None,
        "instruction": (
            "Chunk 0 is included in this prepare response and already available. Read only remaining getCharacterBundleChunk indices from 1 through chunk_count-1 before writing this offscreen character's entrance, speech, message, call, remote reaction or other deliberate action. Use single chunks only."
        ),
    }
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(chunks) == 1
    return result


def get_character_bundle_chunk(
    session_id: str,
    character_id: str,
    read_id: str,
    chunk_index: int,
) -> Dict[str, Any]:
    current_read_id, chunks = _snapshot(session_id, character_id)
    if current_read_id != read_id:
        raise PermissionError("STALE_CHARACTER_READ")
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError(chunk_index)
    return {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "next_chunk_index": None if chunk_index + 1 >= len(chunks) else chunk_index + 1,
    }


def install() -> None:
    """Keep dormant dossiers out of normal packets while advertising the safe retrieval path."""
    global _ORIGINAL_INJECT
    if _ORIGINAL_INJECT is not None:
        return
    from . import session_runtime

    _ORIGINAL_INJECT = session_runtime.inject_required_turn_context

    def wrapped(context, cards, state):
        result = _ORIGINAL_INJECT(context, cards, state)
        result["character_context_instruction"] = (
            "Full character_cards travel only for POV, physically present characters and registered characters explicitly participating in current input/communication. "
            "character_memory is a bounded working copy while lifetime memory stays persistent. character_registry remains the compact registry for every registered character. "
            "Before any other offscreen registered character enters, speaks, sends or receives a message, calls, answers, reacts remotely or otherwise materially acts, call prepareCharacterBundleRead(character_id). Its response already includes chunk 0; read only remaining getCharacterBundleChunk indices before writing the character. "
            "Never call the oversized direct character bundle or direct memory Action."
        )
        contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
        contract["dormant_character_retrieval"] = "bounded_chunked_on_demand"
        contract["remote_communication_requires_loaded_dossier"] = True
        contract["character_read_first_chunk_inline"] = True
        contract["direct_character_bundle_action_allowed"] = False
        result["working_context_contract"] = contract
        return result

    session_runtime.inject_required_turn_context = wrapped
