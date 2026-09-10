from __future__ import annotations

import json
from typing import Any, Dict

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_GUARD_VERSION = 1


def _knowledge_causality_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "source_before_use": True,
        "scene_order_is_causal": True,
        "no_retroactive_justification": True,
        "allowed_sources": [
            "the character's own personal_memory / character_memory",
            "something the character directly saw, heard, read, received or was explicitly told earlier in the current scene while present",
            "an inference whose every premise was already available to that same character before the inference",
        ],
        "forbidden": [
            "using author chronology, recent turns, cards, lore, another character's memory or hidden context as personal knowledge",
            "letting an absent or late-arriving character know an exchange they did not perceive or receive",
            "writing a factual line or confident inference first and inventing the missing source afterwards in narration, memory or explanation",
            "adding a convenient forgotten detail after the fact only to make an already-written conclusion seem logical",
        ],
        "inference_rule": (
            "A character may infer only from premises that character already knows at that exact point in scene order. "
            "Preserve uncertainty: weak premises produce a suspicion/question, not unexplained certainty."
        ),
        "missing_source_behavior": (
            "If the knowledge chain cannot be traced before the line, rewrite the line before commit: remove the knowledge, "
            "turn it into a character-appropriate question or uncertain guess, or first create a real on-page source that the character can perceive. "
            "Never patch the hole after the line with a retroactive explanation."
        ),
        "pre_commit_check": (
            "For every non-trivial factual statement, recognition, inference, question premise and deliberate action by every character, "
            "trace what that character knew immediately before it. Cause/information must precede reaction/conclusion in scene order."
        ),
    }


def _player_text_cleanup_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "raw_input_is_semantic_source": True,
        "correct_in_rendered_scene": [
            "очевидные орфографические ошибки",
            "явные опечатки",
            "очевидную пунктуацию, если исправление не меняет ритм и смысл реплики",
        ],
        "preserve": [
            "смысл и выбранные игроком слова",
            "мат, сленг, просторечие и характерную манеру речи",
            "намеренно разговорные формы, если это стиль, а не очевидная ошибка",
        ],
        "do_not": [
            "не цензурь",
            "не делай речь литературнее или вежливее",
            "не заменяй слова синонимами только ради красоты",
            "не исправляй неоднозначное место, если непонятно, была ли это ошибка",
        ],
        "instruction": (
            "В scene_output технически вычитай пользовательские реплики и ремарки: исправь явную орфографию/опечатки и безопасную пунктуацию, "
            "но сохрани лексику, мат, сленг, тон и смысл. Это редактура формы, а не переписывание голоса POV."
        ),
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
        if isinstance(existing, dict) and existing.get("version") == _GUARD_VERSION:
            return base

        context["scene_logic_guardrails"] = {
            "version": _GUARD_VERSION,
            "knowledge_causality": _knowledge_causality_rule(),
            "player_text_cleanup": _player_text_cleanup_rule(),
            "instruction": (
                "Both child rules are mandatory. Knowledge must have an already-existing character-specific source before use; "
                "rendered player text should be technically corrected without changing voice or meaning."
            ),
        }

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
