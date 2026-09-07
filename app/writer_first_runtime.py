from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import session_runtime, storage
from .npc_intent import active_intents_for
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
WRITER_FIRST_VERSION = 5
WRITER_PACKET_CHARS = 16000
RECENT_FULL_TURNS = 2
CONTINUITY_WINDOW = 15
MAX_WORKING_KNOWLEDGE = 18
MAX_WORKING_EXPERIENCES = 12
MAX_WORKING_DIALOGUE = 12
MAX_HISTORICAL_KNOWLEDGE_CATALOG = 8
MAX_ACTIVE_THREADS = 12

_TERMINAL_THREAD_STATES = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}
_RUNTIME_DROP_KEYS = (
    "pov_participation_contract",
    "npc_agency_contract",
    "relationship_contract",
    "presence_contract",
    "memory_contract",
    "continuity_contract",
    "writer_contract",
)
_REDUNDANT_INSTRUCTION_KEYS = (
    "knowledge_boundary",
    "knowledge_guard",
    "scene_builder_instruction",
    "pov_participation_instruction",
    "npc_agency_instruction",
    "character_context_instruction",
    "relationship_lens_instruction",
    "npc_intent_instruction",
)


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


def _tail(values: Any, limit: int) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    rows = [deepcopy(item) for item in values if isinstance(item, dict)]
    return rows[-limit:]


def _compact_memory(context: Dict[str, Any]) -> None:
    memory = context.get("character_memory")
    if not isinstance(memory, dict):
        return
    for bucket in memory.values():
        if not isinstance(bucket, dict):
            continue
        original_counts = {
            "knowledge": len(bucket.get("knowledge", [])) if isinstance(bucket.get("knowledge"), list) else 0,
            "experiences": len(bucket.get("experiences", [])) if isinstance(bucket.get("experiences"), list) else 0,
            "dialogue_memory": len(bucket.get("dialogue_memory", [])) if isinstance(bucket.get("dialogue_memory"), list) else 0,
        }
        bucket["knowledge"] = _tail(bucket.get("knowledge"), MAX_WORKING_KNOWLEDGE)
        bucket["experiences"] = _tail(bucket.get("experiences"), MAX_WORKING_EXPERIENCES)
        bucket["dialogue_memory"] = _tail(bucket.get("dialogue_memory"), MAX_WORKING_DIALOGUE)

        catalog = bucket.get("historical_knowledge_catalog")
        if isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG:
            bucket["historical_knowledge_catalog"] = deepcopy(catalog[-MAX_HISTORICAL_KNOWLEDGE_CATALOG:])
        older = bucket.get("older_history_available") if isinstance(bucket.get("older_history_available"), dict) else {}
        omitted = {
            "knowledge": max(0, original_counts["knowledge"] - len(bucket["knowledge"])),
            "experiences": max(0, original_counts["experiences"] - len(bucket["experiences"])),
            "dialogue_memory": max(0, original_counts["dialogue_memory"] - len(bucket["dialogue_memory"])),
        }
        if any(omitted.values()) or (isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG):
            bucket["older_history_available"] = {
                "records_omitted": omitted,
                "historical_catalog_truncated": bool(
                    isinstance(catalog, list) and len(catalog) > MAX_HISTORICAL_KNOWLEDGE_CATALOG
                ),
                "retrieval": "prepareCharacterBundleRead",
            }


def _compact_scene_state(value: Any) -> Dict[str, Any]:
    state = deepcopy(value) if isinstance(value, dict) else {}
    state.pop("npc_intents", None)
    return state


def _compact_starting_state(value: Any) -> Dict[str, Any]:
    state = deepcopy(value) if isinstance(value, dict) else {}
    state.pop("npc_intents", None)
    return state


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


def _rolling_turn_context(root) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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


def _separate_future_guidance(context: Dict[str, Any]) -> None:
    direction = context.pop("story_direction", None)
    author = context.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        if direction in (None, {}, []):
            direction = author.pop("story_direction", None)
        else:
            author.pop("story_direction", None)
        author.pop("instruction", None)
        author.pop("knowledge_quarantine", None)
        context["author_context"] = author
    context["future_guidance"] = {
        "story_direction": deepcopy(direction) if direction not in (None, "") else {},
        "status": "future_only_not_history",
    }


def _strip_instruction_noise(context: Dict[str, Any]) -> None:
    for key in _REDUNDANT_INSTRUCTION_KEYS:
        context.pop(key, None)

    lens = context.get("relationship_lens")
    if isinstance(lens, dict):
        lens = deepcopy(lens)
        lens.pop("initialization_instruction", None)
        candidates = lens.get("present_npc_candidates")
        if isinstance(candidates, list):
            for row in candidates:
                if isinstance(row, dict):
                    row.pop("initialization_rule", None)
        context["relationship_lens"] = lens

    policy = context.get("relationship_policy")
    if isinstance(policy, dict):
        policy = deepcopy(policy)
        policy.pop("authoritative_start_snapshot_note", None)
        context["relationship_policy"] = policy

    contract = context.get("working_context_contract")
    if isinstance(contract, dict):
        contract = deepcopy(contract)
        contract.pop("instruction", None)
        context["working_context_contract"] = contract


def _rewrite_context(session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(context)
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})

    for key in _RUNTIME_DROP_KEYS:
        result.pop(key, None)
    _strip_instruction_noise(result)
    _separate_future_guidance(result)
    result["runtime_document_paths"] = {
        "rules": "runtime_rules",
        "scene_builder": "scene_builder",
    }

    result["scene_state"] = _compact_scene_state(result.get("scene_state"))
    result["starting_state"] = _compact_starting_state(result.get("starting_state"))
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

    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "writer_first_version": WRITER_FIRST_VERSION,
            "recent_full_turns": RECENT_FULL_TURNS,
            "continuity_window": CONTINUITY_WINDOW,
            "working_memory_caps": {
                "knowledge": MAX_WORKING_KNOWLEDGE,
                "experiences": MAX_WORKING_EXPERIENCES,
                "dialogue_memory": MAX_WORKING_DIALOGUE,
                "historical_knowledge_catalog": MAX_HISTORICAL_KNOWLEDGE_CATALOG,
            },
            "active_thread_cap": MAX_ACTIVE_THREADS,
            "runtime_documents_per_turn": ["runtime_rules", "scene_builder"],
            "future_guidance_is_not_history": True,
            "npc_intents_are_persistent": True,
            "full_npc_intent_store_in_packet": False,
            "first_packet_chunk_in_prepare_response": True,
        }
    )
    result["working_context_contract"] = contract
    return result


def _next_unread(packet: Dict[str, Any]) -> int | None:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    read = {int(value) for value in packet.get("read_chunks", []) if isinstance(value, int)}
    for index in range(1, len(chunks)):
        if index not in read:
            return index
    return None


def _manifest(packet: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
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
            "first_chunk_included": bool(chunks),
            "next_chunk_index": _next_unread(packet),
            "instruction": "Chunk 0 is included here. Read only remaining unread chunks, then write and commit once.",
        }
    )
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(set(packet.get("read_chunks", []))) >= len(chunks)
    return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    base = dict(_ORIGINAL_PREPARE(session_id, user_input))
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base

        if packet.get("writer_first_version") == WRITER_FIRST_VERSION:
            return _manifest(packet, base)

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
        return _manifest(packet, base)


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
