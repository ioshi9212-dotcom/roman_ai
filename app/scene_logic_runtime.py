from __future__ import annotations

import json
from typing import Any, Dict

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_GUARD_VERSION = 2


def _knowledge_causality_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "source_before_use": True,
        "scene_order_is_causal": True,
        "no_retroactive_justification": True,
        "character_knowledge_is_closed_world": True,
        "allowed_sources": [
            "this character's own personal_memory / character_memory",
            "something this character directly saw, heard, read, received or was explicitly told earlier in the current scene while present",
            "an inference whose every premise was already available to this character from the two sources above",
        ],
        "author_only_not_character_knowledge": [
            "POV questionnaire, NPC questionnaire including that NPC's own questionnaire, character cards and character backstory fields",
            "foundation, foundation_pressure, story_pillars, future_guidance and author plans",
            "chronology, chronology_recent, recent_turns and continuity_turns",
            "lore, hidden_lore, world canon and scene direction",
            "another character's memory, beliefs, relationship state or private information",
        ],
        "forbidden": [
            "treating any author-only source as if a character personally knows its contents",
            "giving an NPC facts from the POV questionnaire merely because the writer packet contains them",
            "giving one NPC facts from another NPC's questionnaire/card/memory",
            "treating a character's own questionnaire/card/backstory as factual awareness unless the same fact exists in that character's memory or was acquired through a real in-story channel",
            "using chronology/recent turns as a character knowledge source unless the same fact is independently present in that character's own memory or was perceived in-scene",
            "an absent/late character knowing an exchange they missed",
            "writing a factual line first and inventing the missing source afterwards",
            "adding a convenient forgotten detail after the fact to justify a conclusion",
        ],
        "questionnaire_rule": (
            "Neither the POV questionnaire nor any NPC questionnaire, including that character's own questionnaire/card/backstory, is personal knowledge. "
            "These author-only fields may shape characterization and plot possibilities, but factual awareness still requires that character's own memory "
            "or a real witnessed/read/heard/received/told channel in story time."
        ),
        "chronology_rule": (
            "Chronology and recent/continuity turns establish authorial canon only. They are never evidence that a specific character knows the event. "
            "For character dialogue or action, require that character's own memory or a current-scene perception/source."
        ),
        "inference_rule": "Every premise must already be known by this character before the inference; weak premises mean suspicion/question, not certainty.",
        "missing_source_behavior": (
            "If the chain is missing before the line, rewrite before commit: remove the knowledge, make it a question/uncertain guess, "
            "or first show a real source the character perceives. Never justify it retroactively."
        ),
        "pre_commit_check": (
            "Cause/information must precede reaction/conclusion. Trace every non-trivial factual statement, recognition, inference, question premise and deliberate action "
            "to this character's own memory or a source they personally perceived before that exact moment. Do not cite chronology, questionnaire/card, foundation, lore or another character's memory."
        ),
    }


def _player_text_cleanup_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "raw_input_is_semantic_source": True,
        "correct_in_rendered_scene": [
            "очевидные орфографические ошибки",
            "явные опечатки",
            "очевидную безопасную пунктуацию",
        ],
        "preserve": [
            "смысл и выбранные игроком слова",
            "мат, сленг, просторечие и характерную манеру речи",
            "намеренно разговорные формы",
        ],
        "do_not": [
            "не цензурь",
            "не делай речь литературнее или вежливее",
            "не заменяй слова синонимами",
            "не исправляй неоднозначное место, если неясно, была ли ошибка",
        ],
        "instruction": "В scene_output исправляй явную орфографию/опечатки и безопасную пунктуацию, сохраняя лексику, мат, сленг, тон и смысл.",
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
            "instruction": "Both rules are mandatory. Character knowledge is closed-world: author canon is not personal knowledge.",
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
