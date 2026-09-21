from __future__ import annotations

import json
from typing import Any, Dict

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_GUARD_VERSION = 3


def _knowledge_causality_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "source_before_use": True,
        "no_retroactive_justification": True,
        "character_knowledge_is_closed_world": True,
        "allowed_sources": [
            "own character_memory",
            "directly saw/heard/read/received or was told through a real in-story channel, including NPC-to-NPC",
            "inference from facts already known to this character",
        ],
        "author_only_not_character_knowledge": [
            "POV/NPC questionnaire, character card/backstory",
            "chronology/recent_turns/continuity_turns",
            "foundation/future_guidance/lore/world canon",
            "another character's memory, beliefs or private information",
        ],
        "rule": (
            "Источник должен существовать до реплики, вывода или действия. "
            "Если источника нет — убери знание, сделай вопрос/догадку или сначала покажи реальный канал. "
            "Не придумывай источник задним числом."
        ),
    }


def _player_input_order_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "source_path": "player_input_map.ordered_segments",
        "left_to_right": True,
        "no_reordering": True,
        "instruction": "ordered_segments: left-to-right; no reordering by kind.",
    }


def _player_text_cleanup_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "rule": "В scene_output исправляй только явные опечатки, орфографию и безопасную пунктуацию. Слова, мат, сленг, тон и смысл не переписывай.",
    }


def _rewrite_packet(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base

        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base

        existing = context.get("scene_logic_guardrails")
        already_front_loaded = bool(context) and next(iter(context)) == "scene_logic_guardrails"
        if isinstance(existing, dict) and existing.get("version") == _GUARD_VERSION and already_front_loaded:
            return base

        guards = {
            "version": _GUARD_VERSION,
            "knowledge_causality": _knowledge_causality_rule(),
            "player_input_order": _player_input_order_rule(),
            "player_text_cleanup": _player_text_cleanup_rule(),
            "instruction": "Соблюдай порядок ввода и границы знаний персонажей.",
        }
        context.pop("scene_logic_guardrails", None)
        context = {"scene_logic_guardrails": guards, **context}

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["scene_logic_guard_version"] = _GUARD_VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "scene_logic_guardrails": True,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def install() -> None:
    global _ORIGINAL_PREPARE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    session_runtime.prepare_turn_packet = _prepare_turn
