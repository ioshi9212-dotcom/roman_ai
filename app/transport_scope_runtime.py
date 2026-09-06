from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict

from . import session_runtime, storage
from .transactional_storage import session_transaction

_ORIGINAL_PREPARE = None


def _strip_legacy_full_payloads(context: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)

    # These legacy fields duplicate complete persistent files and are the main
    # source of long-session packet growth. Persistent files remain untouched.
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

    result["character_context_instruction"] = (
        "character_cards and character_memory are complete for POV, present cast and characters resolved from current input. "
        "character_registry is the compact registry for every registered character. Dormant full dossiers remain persisted in Railway but are not retransmitted. "
        "If an offscreen registered character whose dossier is absent must enter or materially act, call prepareCharacterBundleRead, then read every getCharacterBundleChunk individually before writing that character. "
        "Do not use direct oversized character bundle or memory Actions."
    )
    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "turn_packet_is_scene_scoped": True,
            "legacy_full_state_memory_chronology_in_packet": False,
            "dormant_full_dossiers_in_packet": False,
            "dormant_character_retrieval": "chunked_on_demand",
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

    # Multiple simultaneous/retried prepareTurn calls for the same turn must not
    # replace each other's packet_id. Serialize check + creation across requests.
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
        context = _strip_legacy_full_payloads(json.loads(raw))
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[i : i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = []
        packet["transport_scope_version"] = 3
        storage._write_json(root / "turn_packet.json", packet)
        manifest["chunk_count"] = len(chunks)
        manifest["total_chars"] = len(text)
        manifest["working_context"] = True
        manifest["relevant_character_ids"] = [str(value) for value in context.get("relevant_character_ids", []) if value]
        manifest["reused_pending_packet"] = False
        manifest["instruction"] = (
            "Read every turn packet chunk individually before writing. The packet is scene-scoped; dormant full dossiers remain safely persisted. "
            "Use prepareCharacterBundleRead plus getCharacterBundleChunk for an offscreen registered character whose dossier is absent."
        )
        return manifest


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
