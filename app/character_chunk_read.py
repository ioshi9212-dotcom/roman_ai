from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from .character_access import get_character_bundle
from .scene_compaction_runtime import active_memory_records


CHARACTER_CHUNK_CHARS = 12000
CHARACTER_WORKING_KNOWLEDGE = 18
CHARACTER_WORKING_EXPERIENCES = 12
CHARACTER_WORKING_DIALOGUE = 12
CHARACTER_HISTORICAL_CATALOG = 8
CHARACTER_MEMORY_TEXT_CHARS = 900
MAX_INTENT_SOURCE_FACTS = 12
_ORIGINAL_INJECT = None


def _bundle_knowledge_firewall(character_id: str) -> Dict[str, Any]:
    return {
        "mandatory": True,
        "character_id": character_id,
        "card_is_author_only": True,
        "allowed_sources": ["personal_memory", "real perception/contact", "inference from known facts"],
        "forbidden_sources": ["card/backstory", "chronology", "lore/foundation/future", "other private data"],
        "instruction": "CARD — авторский канон. Фактическое знание персонажа — только memory/реальный источник.",
    }


def _turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        return int(item.get("learned_turn") or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _recency_turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        if item.get("last_learned_turn") not in (None, ""):
            return int(item["last_learned_turn"])
    except (TypeError, ValueError):
        pass
    return _turn(item)


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


def _bound_memory_value(value: Any) -> Any:
    """Bound writer-facing memory prose without touching identifiers or persistent storage."""
    if isinstance(value, str):
        if len(value) <= CHARACTER_MEMORY_TEXT_CHARS:
            return value
        return value[:CHARACTER_MEMORY_TEXT_CHARS] + "…[full memory text remains in persistent storage]"
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            # IDs must remain exact so intent source references and audit provenance still work.
            if key in {"fact_id", "event_id", "topic_id", "id", "character_id", "source_turn"}:
                result[key] = deepcopy(item)
            else:
                result[key] = _bound_memory_value(item)
        return result
    if isinstance(value, list):
        return [_bound_memory_value(item) for item in value]
    return deepcopy(value)


def _tail(values: Any, limit: int) -> List[Dict[str, Any]]:
    rows = active_memory_records(values)
    rows.sort(key=_recency_turn)
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
    all_knowledge = active_memory_records(memory.get("knowledge"))

    # Knowledge is factual authority for this character. Do not replace old facts
    # with a tiny historical catalog: the bundle is chunked, so all active facts can
    # be transported safely.
    knowledge = deepcopy(all_knowledge)

    experiences = _tail(memory.get("experiences"), CHARACTER_WORKING_EXPERIENCES)
    dialogue = _tail(memory.get("dialogue_memory"), CHARACTER_WORKING_DIALOGUE)
    return {
        "knowledge": knowledge,
        "experiences": _bound_memory_value(experiences),
        "dialogue_memory": _bound_memory_value(dialogue),
        "historical_knowledge_catalog": [],
        "knowledge_complete_in_transport": True,
        "knowledge_text_not_truncated_in_transport": True,
        "persistent_counts": {
            "knowledge": len(memory.get("knowledge", [])) if isinstance(memory.get("knowledge"), list) else 0,
            "experiences": len(memory.get("experiences", [])) if isinstance(memory.get("experiences"), list) else 0,
            "dialogue_memory": len(memory.get("dialogue_memory", [])) if isinstance(memory.get("dialogue_memory"), list) else 0,
        },
        "canonical_active_counts": {
            "knowledge": len(all_knowledge),
            "experiences": len(active_memory_records(memory.get("experiences"))),
            "dialogue_memory": len(active_memory_records(memory.get("dialogue_memory"))),
        },
        "older_history_available": bool(
            len(active_memory_records(memory.get("experiences"))) > len(experiences)
            or len(active_memory_records(memory.get("dialogue_memory"))) > len(dialogue)
        ),
        "oversized_record_text_bounded_in_transport": True,
        "memory_text_chars_max": CHARACTER_MEMORY_TEXT_CHARS,
    }


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    full = get_character_bundle(session_id, character_id)
    return {
        "knowledge_firewall": _bundle_knowledge_firewall(character_id),
        "character_id": character_id,
        "card": deepcopy(full.get("card", {})),
        "current_state": deepcopy(full.get("current_state", {})),
        "pov_familiarity": deepcopy(full.get("pov_familiarity")),
        "personal_memory": _working_memory(full),
        "relationship_to_pov": deepcopy(full.get("relationship_to_pov")),
        "active_intents": deepcopy(full.get("active_intents", [])),
        "working_bundle": True,
        "persistent_lifetime_memory_complete": True,
        "instruction": "CARD — авторский контекст. personal_memory.knowledge передаётся полностью и является фактическим источником персонажа; experiences/dialogue могут быть bounded.",
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
        "instruction": "Chunk 0 уже включён. Прочитай остальные chunks до участия персонажа.",
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
        result["character_context_instruction"] = "Offscreen NPC перед участием → prepareCharacterBundleRead и все оставшиеся chunks. Прямой oversized bundle не использовать."
        contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
        contract["dormant_character_retrieval"] = "bounded_chunked_on_demand"
        contract["remote_communication_requires_loaded_dossier"] = True
        contract["character_read_first_chunk_inline"] = True
        contract["direct_character_bundle_action_allowed"] = False
        result["working_context_contract"] = contract
        return result

    session_runtime.inject_required_turn_context = wrapped
