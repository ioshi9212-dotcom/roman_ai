import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import knowledge_firewall_runtime as firewall
from app import operation_service, storage


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "knowledge_guard_v7",
        "title": "Knowledge guard v7",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
            {"character_id": "liam", "name": "Лиам", "role": "major"},
            {"character_id": "veil", "name": "Вейл", "role": "major"},
        ],
        "lore": {},
        "knowledge": {
            "characters": {
                "elena": {"knowledge": []},
                "liam": {"knowledge": []},
                "veil": {"knowledge": []},
            }
        },
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {
                "date": "2026-07-20",
                "time": "22:00",
                "location": "комната",
                "present_characters": ["elena", "liam"],
            },
        },
    }


def _prepare(session_id: str):
    packet = operation_service.prepare_turn_request(
        session_id,
        "(сидеть рядом с Лиамом)",
        request_id="knowledge-v7-request",
        scene_archive_capable=False,
        knowledge_review_capable=True,
        strict_knowledge_capable=True,
        replace_pending=False,
    )
    for index in packet.get("pending_turn", {}).get("unread_chunk_indices", []):
        storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)
    return packet


def _payload(scene_output: str, usage):
    return {
        "packet_id": "direct-validator",
        "user_input": "(сидеть рядом с Лиамом)",
        "scene_output": scene_output,
        "extracted": {
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "knowledge_trace_complete": True,
            "turn_knowledge": [],
            "knowledge_usage": usage,
            "chronology": [],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
        },
    }


def test_lower_block_options_are_not_part_of_knowledge_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            (
                "Что я могу сделать:\n"
                "1. Напомнить Лиаму о его обещании.\n"
                "Что я могу сказать:\n"
                "1. «Сто раз я, конечно, пизданула.»\n"
                "Что я могу подумать:\n"
                "1. Вечером Маркус. Прекрасная шпионская карьера."
            ),
            [],
        )

        firewall._validate_knowledge_commit(sid, payload)


def test_real_speech_still_uses_strict_knowledge_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — То синее платье тебе идёт.",
            [],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_USAGE_COVERAGE_MISMATCH"


def test_speech_about_unknown_dress_remains_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — То синее платье тебе идёт.",
            [{"unit_id": "speech:1", "character_id": "liam", "fact_free": True}],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_FACT_FREE_SENSITIVE"
        assert "clothing" in exc.value.detail["reason"]


def test_speech_about_unknown_scheduled_contact_remains_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — Завтра ты к Вейлу во сколько?",
            [{"unit_id": "speech:1", "character_id": "liam", "fact_free": True}],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_FACT_FREE_SENSITIVE"
        assert "scheduled_contact" in exc.value.detail["reason"]
