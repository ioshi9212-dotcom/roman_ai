from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict

from . import session_runtime, storage
from .transactional_storage import session_transaction

_ORIGINAL_PREPARE = None
_MAX_THREAD_TEXT = 1200
_MAX_MEMORY_FIELD_TEXT = 700


def _bounded_transport_value(value: Any, *, max_text: int = _MAX_THREAD_TEXT) -> Any:
    if isinstance(value, str):
        if len(value) <= max_text:
            return value
        return value[:max_text] + "…[full value remains in persistent storage]"
    if isinstance(value, dict):
        return {str(key): _bounded_transport_value(item, max_text=max_text) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_transport_value(item, max_text=max_text) for item in value]
    return deepcopy(value)


def _compact_starting_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = deepcopy(value)
    for key in ("relationships", "relationship_documents", "relationship_schemas", "threads", "characters"):
        result.pop(key, None)
    return _bounded_transport_value(result)


def _compact_character_memory(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _bounded_transport_value(value, max_text=_MAX_MEMORY_FIELD_TEXT)


def _relationship_snapshot_from_persistent_state(state: Dict[str, Any]) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    elif isinstance(present, str):
        present = [present]
    elif not isinstance(present, list):
        present = []
    ids = []
    for value in present:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            ids.append(str(value))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    result: Dict[str, Any] = {}
    for owner_id in dict.fromkeys(ids):
        if not owner_id or owner_id == pov_id:
            continue
        relation = relationships.get(owner_id)
        metrics = {
            str(key): number
            for key, number in relation.items()
            if isinstance(relation, dict) and isinstance(number, (int, float)) and not isinstance(number, bool)
        } if isinstance(relation, dict) else {}
        result[owner_id] = {"metrics": metrics, "has_saved_baseline": bool(metrics)}
    return result


def _strip_legacy_full_payloads(context: Dict[str, Any], *, persistent_state: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)

    for key in (
        "state",
        "memory",
        "chronology",
        "characters",
        "all_character_cards",
        "memory_full",
        "source_full",
        "state_full",
        "scene_character_cards",
        "scene_character_memory",
        "character_registry_index",
    ):
        result.pop(key, None)

    source = result.get("source")
    if isinstance(source, dict):
        source = deepcopy(source)
        source.pop("characters", None)
        result["source"] = source

    novel_source = result.get("novel_source")
    if isinstance(novel_source, dict):
        novel_source = deepcopy(novel_source)
        novel_source.pop("characters", None)
        result["novel_source"] = novel_source

    author = result.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        for key in (
            "state",
            "memory",
            "chronology",
            "characters",
            "all_character_cards",
            "memory_full",
            "source_full",
            "state_full",
            "character_cards",
            "chronology_recent",
            "recent_turns",
        ):
            author.pop(key, None)
        result["author_context"] = author

    result["starting_state"] = _compact_starting_state(result.get("starting_state"))
    result["character_memory"] = _compact_character_memory(result.get("character_memory"))
    if "active_threads" in result:
        result["active_threads"] = _bounded_transport_value(result.get("active_threads"))

    policy = result.get("relationship_policy") if isinstance(result.get("relationship_policy"), dict) else {}
    policy["authoritative_start_snapshot"] = _relationship_snapshot_from_persistent_state(persistent_state)
    policy["authoritative_start_snapshot_note"] = (
        "Compact diagnostic start values for physically present NPCs, rebuilt from persistent state before transport compaction. "
        "relationship_lens + relationship_contract remain authoritative."
    )
    result["relationship_policy"] = policy

    result["character_context_instruction"] = (
        "Full character_cards are transported only for POV, physically present characters and registered characters explicitly participating in the current input or communication. "
        "character_memory is a bounded working copy; complete lifetime memory remains persisted. Oversized memory text may be shortened in transport only; load the full character bundle if exact omitted wording matters. "
        "character_registry stays available for every registered character. If any other offscreen registered character must enter, speak, message, call, answer, react remotely or otherwise materially act, call prepareCharacterBundleRead and read every getCharacterBundleChunk individually before writing that character. "
        "Do not use direct oversized character bundle or memory Actions."
    )
    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "turn_packet_is_scene_scoped": True,
            "legacy_full_state_memory_chronology_in_packet": False,
            "dormant_full_dossiers_in_packet": False,
            "lifetime_memory_in_packet": False,
            "oversized_memory_text_bounded_in_transport": True,
            "full_relationship_documents_in_packet": False,
            "full_starting_state_in_packet": False,
            "active_threads_text_is_bounded": True,
            "dormant_character_retrieval": "chunked_on_demand",
            "remote_communication_requires_loaded_dossier": True,
            "persistent_storage_is_complete": True,
            "same_pending_turn_prepare_is_idempotent": True,
        }
    )
    result["working_context_contract"] = contract
    return result


def _pending_manifest(packet: Dict[str, Any]) -> Dict[str, Any]:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    return {
        "packet_id": packet.get("packet_id"),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "chunk_count": len(chunks),
        "total_chars": sum(len(str(chunk)) for chunk in chunks),
        "relevant_character_ids": [str(value) for value in packet.get("relevant_character_ids", []) if value],
        "working_context": True,
        "reused_pending_packet": True,
        "read_chunks": list(packet.get("read_chunks", [])),
        "instruction": (
            "This exact pending turn was already prepared, so the existing packet_id was reused instead of invalidating in-progress chunk reads. "
            "Continue reading any unread packet chunks individually, then commit once."
        ),
    }


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        expected_turn = int(meta.get("turn_number", 0) or 0) + 1
        packet = storage._read_json(root / "turn_packet.json", {})
        if (
            isinstance(packet, dict)
            and packet.get("packet_id")
            and int(packet.get("prepared_for_turn", 0) or 0) == expected_turn
            and packet.get("user_input") == user_input
            and isinstance(packet.get("chunks"), list)
            and packet.get("chunks")
        ):
            return _pending_manifest(packet)

        manifest = dict(_ORIGINAL_PREPARE(session_id, user_input))
        packet = storage._read_json(root / "turn_packet.json", {})
        raw = "".join(packet.get("chunks", []))
        if not raw:
            return manifest
        persistent_state = storage._read_json(root / "state.json", {})
        context = _strip_legacy_full_payloads(json.loads(raw), persistent_state=persistent_state)
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[i : i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = []
        packet["transport_scope_version"] = 6
        storage._write_json(root / "turn_packet.json", packet)
        manifest["chunk_count"] = len(chunks)
        manifest["total_chars"] = len(text)
        manifest["working_context"] = True
        manifest["relevant_character_ids"] = [str(value) for value in context.get("relevant_character_ids", []) if value]
        manifest["reused_pending_packet"] = False
        manifest["instruction"] = (
            "Read every turn packet chunk individually before writing. The packet is a bounded scene working set; complete persistent data remains in Railway. "
            "Load an absent registered character bundle before any entrance, speech, message, call, remote reaction or other material action."
        )
        return manifest


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
