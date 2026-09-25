from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import HTTPException

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_GUARD_VERSION = 7


def _knowledge_causality_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "applies_to": "real speech only",
        "source_before_use": True,
        "no_retroactive_justification": True,
        "character_knowledge_is_closed_world": True,
        "allowed_sources": [
            "dialogue_frames[character_id].knowledge_path",
            "speaker self-known facts from dialogue_frames[character_id].self_card_path via source_self_paths",
            "earlier turn_knowledge for the same character_id, including valid canon_fill",
        ],
        "author_only_not_character_knowledge": [
            "questionnaire and another character's card facts",
            "own card branches marked unknown_to_self/hidden_from_self/author_only",
            "chronology/recent_turns/continuity_turns/scene_history",
            "foundation/future_guidance/lore/world canon",
            "another character's memory or private information",
        ],
        "rule": (
            "Перед репликой используй dialogue_frame говорящего: knowledge_path, собственные self-known card paths "
            "или более ранний turn_knowledge. Если личная деталь нигде не задана, допустим canon_fill; "
            "уже заданный канон не переписывай. Чужой/author context не является источником реплики."
        ),
    }


def _knowledge_review_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "applies_to": "real speech only",
        "older_memory_retrieval": "prepareCharacterBundleRead(character_id)",
        "rule": (
            "После сцены перечитай каждую реальную реплику отдельно. Зафиксируй exact speech_text и все factual claims/presuppositions; "
            "каждый claim должен иметь источник говорящего. Нет источника до реплики — перепиши только эту реплику. "
            "После полной проверки поставь extracted.knowledge_reviewed=true."
        ),
    }


def _player_input_order_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "source_path": "player_input_map.ordered_segments",
        "left_to_right": True,
        "no_reordering": True,
        "sequential_execution": True,
        "world_may_interleave": True,
        "no_batching_player_segments": True,
        "instruction": (
            "Исполняй ordered_segments строго слева направо как последовательность моментов сцены. "
            "Реплика вне скобок произносится в своей позиции; содержимое ( ) происходит/мыслится в своей позиции ДО следующего сегмента. "
            "Не склеивай все реплики POV вместе и не переноси действия/мысли после более поздних реплик. "
            "Между соседними сегментами могут естественно вклиниваться реакция NPC, пауза или событие, если это логично; "
            "не вставляй реакцию искусственно после каждого сегмента."
        ),
    }


def _player_text_cleanup_rule() -> Dict[str, Any]:
    return {
        "mandatory": True,
        "rule": "Реплики POV из user_input при переносе в scene_output: исправляй явные опечатки, орфографию и безопасную пунктуацию; слова, мат, сленг, тон и смысл не переписывай.",
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
        first_key = next(iter(context)) if context else None
        already_front_loaded = first_key in {"scene_logic_guardrails", "knowledge_firewall_v5"}
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
    if (
        not isinstance(packet, dict)
        or int(packet.get("scene_logic_guard_version", 0) or 0) < _GUARD_VERSION
        or not bool(packet.get("knowledge_review_capable"))
    ):
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
                "Проверь каждую реальную реплику отдельно по dialogue_frame говорящего. "
                "Каждый factual claim/presupposition должен ссылаться на knowledge_path или более ранний turn_knowledge этого персонажа. "
                "Нет источника — перепиши реплику и повтори тот же commit с extracted.knowledge_reviewed=true."
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
