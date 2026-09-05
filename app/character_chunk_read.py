from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .character_access import get_character_bundle


CHARACTER_CHUNK_CHARS = 6000
_ORIGINAL_INJECT = None


def _snapshot(session_id: str, character_id: str) -> tuple[str, List[str]]:
    bundle = get_character_bundle(session_id, character_id)
    text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    read_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    chunks = [text[i : i + CHARACTER_CHUNK_CHARS] for i in range(0, len(text), CHARACTER_CHUNK_CHARS)] or ["{}"]
    return read_id, chunks


def prepare_character_bundle_read(session_id: str, character_id: str) -> Dict[str, Any]:
    read_id, chunks = _snapshot(session_id, character_id)
    return {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_count": len(chunks),
        "chunk_chars_max": CHARACTER_CHUNK_CHARS,
        "instruction": "Read every character bundle chunk from index 0 through chunk_count-1 before writing this offscreen character into the scene. Use single chunks only.",
    }


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
            "character_cards and character_memory are complete for POV, present cast and characters resolved from current input. "
            "character_registry remains the compact registry for every registered character. Dormant dossiers stay fully persisted but do not ride every turn packet. "
            "Before an offscreen registered character whose full dossier is absent enters or materially acts, call prepareCharacterBundleRead(character_id), then read every getCharacterBundleChunk from 0 through chunk_count-1 using single chunks. "
            "Never call the oversized direct character bundle or direct memory Action."
        )
        contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
        contract["dormant_character_retrieval"] = "chunked_on_demand"
        contract["direct_character_bundle_action_allowed"] = False
        result["working_context_contract"] = contract
        return result

    session_runtime.inject_required_turn_context = wrapped
