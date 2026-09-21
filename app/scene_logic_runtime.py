from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import HTTPException

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_GUARD_VERSION = 4


def _knowledge_causality_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "applies_to": "POV and every NPC",
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
            "chronology/recent_turns/continuity_turns/scene_history",
            "foundation/future_guidance/lore/world canon",
            "another character's memory, beliefs or private information",
        ],
        "rule": (
            "POV и NPC знают факт только из своей памяти или реального источника. "
            "Chronology/scene_history/card/foundation/lore — авторский канон, не знание персонажа. "
            "Нет источника — перепиши сцену; не придумывай источник задним числом."
        ),
    }


def _knowledge_review_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "applies_to": "POV and every NPC",
        "older_memory_retrieval": "prepareCharacterBundleRead(character_id)",
        "rule": (
            "Перед commit проверь scene_output: каждую реплику, мысль, узнавание и осознанное действие с фактом сверяй с личной памятью "
            "или информацией, реально полученной этим персонажем. Если working memory недостаточно, догрузи его bundle. "
            "Факт без источника перепиши. После 0 нарушений передай extracted.knowledge_reviewed=true."
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
            "knowledge_review": _knowledge_review_rule(),
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


def _require_knowledge_review(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or int(packet.get("scene_logic_guard_version", 0) or 0) < _GUARD_VERSION:
        return
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    if extracted.get("knowledge_reviewed") is True:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "KNOWLEDGE_REVIEW_REQUIRED",
            "packet_id": packet.get("packet_id"),
            "instruction": (
                "Проверь знания POV и всех NPC в текущем scene_output. Для каждого факта нужен источник в личной памяти "
                "или реально полученная в сцене информация. Chronology/scene_history/card/foundation/lore не являются знанием персонажа. "
                "Факт без источника перепиши, затем повтори тот же commit с extracted.knowledge_reviewed=true. Новый prepareTurn не вызывай."
            ),
        },
    )


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_knowledge_review(session_id, payload)
    return _ORIGINAL_COMMIT(session_id, payload)


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
