from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import session_runtime, storage
from .npc_intent import active_intents_for
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
WRITER_FIRST_VERSION = 1
WRITER_PACKET_CHARS = 16000
RECENT_FULL_TURNS = 2
CONTINUITY_WINDOW = 15
MAX_HISTORICAL_KNOWLEDGE_CATALOG = 24
MAX_ACTIVE_THREADS = 12
RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"

_TERMINAL_THREAD_STATES = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}
_RUNTIME_DROP_KEYS = (
    "pov_participation_contract",
    "npc_agency_contract",
    "relationship_contract",
    "presence_contract",
    "memory_contract",
    "continuity_contract",
)


def _writer_contract() -> str:
    return (RUNTIME_DIR / "writer_contract.md").read_text(encoding="utf-8")


def _compact_cast_index(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    keep = {
        "character_id", "name", "full_name", "role", "is_pov", "present", "location", "pov_familiarity",
        "last_seen_turn", "last_interaction_turn",
    }
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        compact = {key: deepcopy(row[key]) for key in keep if key in row and row[key] not in (None, "", [], {})}
        if compact:
            result.append(compact)
    return result


def _thread_status(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("status", "state", "phase"):
            if value.get(key) not in (None, ""):
                return str(value[key]).casefold().strip()
    return ""


def _thread_priority(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get("priority")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    lookup = {"critical": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}
    return lookup.get(str(raw or "").casefold(), 0)


def _active_threads(value: Any) -> Any:
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if _thread_status(item) in _TERMINAL_THREAD_STATES:
                continue
            rows.append((str(key), deepcopy(item)))
        rows.sort(key=lambda pair: _thread_priority(pair[1]), reverse=True)
        return {key: item for key, item in rows[:MAX_ACTIVE_THREADS]}
    if isinstance(value, list):
        rows = [deepcopy(item) for item in value if _thread_status(item) not in _TERMINAL_THREAD_STATES]
        rows.sort(key=_thread_priority, reverse=True)
        return rows[:MAX_ACTIVE_THREADS]
    return value


def _compact_memory(context: Dict[str, Any]) -> None:
    memory = context.get("character_memory")
    if not isinstance(memory, dict):
        return
    for bucket in memory.values():
        if not isinstance(bucket, dict):
            continue
        catalog = bucket.get("historical_knowledge_catalog")
        if isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG:
            bucket["historical_knowledge_catalog"] = deepcopy(catalog[-MAX_HISTORICAL_KNOWLEDGE_CATALOG:])
            older = bucket.get("older_history_available") if isinstance(bucket.get("older_history_available"), dict) else {}
            older["historical_catalog_truncated"] = True
            older["historical_catalog_records_omitted"] = len(catalog) - MAX_HISTORICAL_KNOWLEDGE_CATALOG
            older["retrieval"] = "prepareCharacterBundleRead -> getCharacterBundleChunk"
            bucket["older_history_available"] = older


def _compact_full_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: deepcopy(turn[key])
        for key in ("turn_number", "user_input", "scene_output", "extracted")
        if key in turn
    }


def _compact_continuity_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    result: Dict[str, Any] = {
        "turn_number": int(turn.get("turn_number", 0) or 0),
        "user_input": str(turn.get("user_input") or "")[:700],
    }
    chronology = extracted.get("chronology")
    if isinstance(chronology, list) and chronology:
        result["chronology"] = deepcopy(chronology[:4])
    for key in ("presence_updates", "npc_intent_updates", "relationship_updates"):
        value = extracted.get(key)
        if isinstance(value, list) and value:
            result[key] = deepcopy(value[:8])
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current = state_patch.get("current") if isinstance(state_patch.get("current"), dict) else {}
    if current:
        result["current_patch"] = deepcopy(current)
    if len(result) == 2:
        scene = " ".join(str(turn.get("scene_output") or "").split())
        if scene:
            result["scene_tail"] = scene[-700:]
    return result


def _rolling_turn_context(root: Path) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    turns = storage._read_turns(root)
    window = turns[-CONTINUITY_WINDOW:]
    full = [_compact_full_turn(turn) for turn in window[-RECENT_FULL_TURNS:]]
    continuity = [_compact_continuity_turn(turn) for turn in window[:-RECENT_FULL_TURNS]]
    return full, continuity


def _scene_ids(context: Dict[str, Any]) -> List[str]:
    ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
    for row in context.get("character_cards", []) if isinstance(context.get("character_cards"), list) else []:
        if isinstance(row, dict) and row.get("character_id"):
            ids.append(str(row["character_id"]))
    return list(dict.fromkeys(ids))


def _rewrite_context(session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(context)
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})

    for key in _RUNTIME_DROP_KEYS:
        result.pop(key, None)
    result["writer_contract"] = _writer_contract()
    result["runtime_document_paths"] = {
        "rules": "runtime_rules",
        "scene_builder": "scene_builder",
        "writer_contract": "writer_contract",
    }

    recent, continuity = _rolling_turn_context(root)
    result["recent_turns"] = recent
    result["continuity_turns"] = continuity
    result["active_threads"] = _active_threads(result.get("active_threads"))
    result["cast_index"] = _compact_cast_index(result.get("cast_index"))
    _compact_memory(result)

    character_ids = _scene_ids(result)
    result["npc_active_intents"] = active_intents_for(
        state,
        character_ids,
        current_turn=int(meta.get("turn_number", 0) or 0),
    )
    result["npc_intent_instruction"] = (
        "Memory must cause behavior when appropriate. npc_active_intents are unresolved character-owned follow-ups, suspicions, promises, investigations and goals. "
        "An eligible intent may drive initiative without POV reminding the NPC. Do not repeat it mechanically; character, opportunity, urgency, relationship and elapsed game time decide whether to pursue it now. "
        "When an NPC genuinely forms, advances, resolves or abandons a durable future-facing motive, persist it through extracted.npc_intent_updates."
    )

    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "writer_first_version": WRITER_FIRST_VERSION,
            "recent_full_turns": RECENT_FULL_TURNS,
            "continuity_window": CONTINUITY_WINDOW,
            "historical_knowledge_catalog_cap": MAX_HISTORICAL_KNOWLEDGE_CATALOG,
            "active_thread_cap": MAX_ACTIVE_THREADS,
            "runtime_documents_per_turn": ["runtime_rules", "scene_builder", "writer_contract"],
            "npc_intents_are_persistent": True,
            "first_packet_chunk_in_prepare_response": True,
        }
    )
    result["working_context_contract"] = contract
    return result


def _manifest(packet: Dict[str, Any], base: Dict[str, Any], *, include_first: bool) -> Dict[str, Any]:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    result = dict(base)
    result.update(
        {
            "packet_id": packet.get("packet_id"),
            "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
            "chunk_count": len(chunks),
            "total_chars": sum(len(str(chunk)) for chunk in chunks),
            "writer_first": True,
            "writer_first_version": WRITER_FIRST_VERSION,
            "chunk_chars_max": WRITER_PACKET_CHARS,
            "first_chunk_included": bool(include_first and chunks),
            "next_chunk_index": 1 if include_first and len(chunks) > 1 else None,
            "instruction": (
                "The prepareTurn response includes chunk 0 when first_chunk_included=true; count it as already read. Read only remaining chunks individually in order. "
                "Then write and commit the scene once. The packet is a bounded writer-sized working set; complete persistent canon remains in Railway."
            ),
        }
    )
    if include_first and chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(chunks) == 1
    return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    base = dict(_ORIGINAL_PREPARE(session_id, user_input))
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base

        if packet.get("writer_first_version") == WRITER_FIRST_VERSION:
            return _manifest(packet, base, include_first=False)

        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        context = _rewrite_context(session_id, json.loads(raw))
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[index:index + WRITER_PACKET_CHARS] for index in range(0, len(text), WRITER_PACKET_CHARS)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["writer_first_version"] = WRITER_FIRST_VERSION
        packet["writer_first_payload_chars"] = len(text)
        storage._write_json(root / "turn_packet.json", packet)
        return _manifest(packet, base, include_first=True)


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
