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


def _novel(*, liam_knows_meeting: bool = False):
    liam_knowledge = [
        {
            "fact_id": "liam_knows_old_veil_context",
            "fact": "Лиам слышал, что Вейл когда-то привёз Елену Высоцкую на базу.",
        },
        {
            "fact_id": "liam_knows_elena_name",
            "fact": "Лиам знает, что девушку зовут Елена.",
        },
    ]
    if liam_knows_meeting:
        liam_knowledge.append(
            {
                "fact_id": "liam_knows_veil_meeting",
                "fact": "Елена прямо сказала Лиаму, что завтра встречается с Вейлом.",
            }
        )

    return {
        "novel_id": "claim_guard_v6",
        "title": "Claim guard v6",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
            {"character_id": "liam", "name": "Лиам", "role": "major"},
            {"character_id": "veil", "name": "Вейл", "role": "major"},
        ],
        "lore": {},
        "foundation": {
            "facts": [
                {
                    "fact_id": "author_future_veil_meeting",
                    "text": "Завтра у Елены назначена встреча с Вейлом.",
                    "stored_in": ["story_direction.current_phase"],
                    "story_use": "future",
                },
                {
                    "fact_id": "author_blue_dress",
                    "text": "Для завтрашнего вечера Елена выбрала синее платье.",
                    "stored_in": ["characters.elena.private_notes"],
                    "story_use": "future",
                },
            ],
            "hooks": [],
            "story_pillars": [],
        },
        "story_direction": {
            "current_phase": "Завтра у Елены встреча с Вейлом. Она выбрала синее платье.",
        },
        "knowledge": {
            "characters": {
                "elena": {
                    "knowledge": [
                        {
                            "fact_id": "elena_knows_own_veil_meeting",
                            "fact": "Елена знает, что завтра встречается с Вейлом.",
                        }
                    ]
                },
                "liam": {"knowledge": liam_knowledge},
                "veil": {"knowledge": []},
            }
        },
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {
                "date": "2026-07-20",
                "time": "22:00",
                "location": "комната Лиама",
                "present_characters": ["elena", "liam"],
            },
        },
    }


def _prepare(session_id: str):
    packet = operation_service.prepare_turn_request(
        session_id,
        "(лежать рядом с Лиамом)",
        request_id="claim-guard-request",
        scene_archive_capable=False,
        knowledge_review_capable=True,
        strict_knowledge_capable=True,
        replace_pending=False,
    )
    for index in packet.get("pending_turn", {}).get("unread_chunk_indices", []):
        storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)
    return packet


def _payload(scene_output: str, usage, turn_knowledge=None):
    return {
        "packet_id": "direct-validator",
        "user_input": "(лежать рядом с Лиамом)",
        "scene_output": scene_output,
        "extracted": {
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "knowledge_trace_complete": True,
            "turn_knowledge": turn_knowledge or [],
            "knowledge_usage": usage,
            "chronology": [],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
        },
    }


def test_unknown_future_contact_claim_requires_speaker_source():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — Кстати. Завтра ты к Вейлу во сколько?",
            [{
                "unit_id": "speech:1",
                "character_id": "liam",
                "speech_text": "Кстати. Завтра ты к Вейлу во сколько?",
                "claims_reviewed": True,
                "claims": [{"claim": "У Елены завтра есть контакт с Вейлом."}],
            }],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_CLAIM_SOURCE_REQUIRED"


def test_old_veil_fact_does_not_cover_new_meeting_claim():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — Кстати. Завтра ты к Вейлу во сколько?",
            [
                {
                    "unit_id": "speech:1",
                    "character_id": "liam",
                    "speech_text": "Кстати. Завтра ты к Вейлу во сколько?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "У Елены завтра есть контакт с Вейлом.",
                        "source_fact_ids": ["liam_knows_old_veil_context"],
                    }],
                }
            ],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_SOURCE_NOT_RELEVANT"
        assert "вейл" in exc.value.detail["missing_contact_targets"]


def test_dialogue_memory_id_cannot_be_used_as_knowledge_source():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — Кстати. Завтра ты к Вейлу во сколько?",
            [
                {
                    "unit_id": "speech:1",
                    "character_id": "liam",
                    "speech_text": "Кстати. Завтра ты к Вейлу во сколько?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "У Елены завтра есть контакт с Вейлом.",
                        "source_event_ids": ["dialogue_t593_elena_confirms_marcus_meeting"],
                    }],
                }
            ],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_USAGE_FORBIDDEN_SOURCE"


def test_unknown_dress_claim_requires_speaker_source():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — То синее платье тебе идёт.",
            [{
                "unit_id": "speech:1",
                "character_id": "liam",
                "speech_text": "То синее платье тебе идёт.",
                "claims_reviewed": True,
                "claims": [{"claim": "У Елены есть синее платье."}],
            }],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_CLAIM_SOURCE_REQUIRED"


def test_unrelated_source_does_not_cover_unknown_dress():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — То синее платье тебе идёт.",
            [
                {
                    "unit_id": "speech:1",
                    "character_id": "liam",
                    "speech_text": "То синее платье тебе идёт.",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "У Елены есть синее платье.",
                        "source_fact_ids": ["liam_knows_elena_name"],
                    }],
                }
            ],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_SOURCE_NOT_RELEVANT"
        assert "clothing" in exc.value.detail["missing_topics"]


def test_real_meeting_knowledge_allows_veil_question():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel(liam_knows_meeting=True))["session_id"]
        _prepare(sid)

        payload = _payload(
            "**Лиам** — Кстати. Завтра ты к Вейлу во сколько?",
            [
                {
                    "unit_id": "speech:1",
                    "character_id": "liam",
                    "speech_text": "Кстати. Завтра ты к Вейлу во сколько?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Елена завтра встречается с Вейлом.",
                        "source_fact_ids": ["liam_knows_veil_meeting"],
                    }],
                }
            ],
        )

        firewall._validate_knowledge_commit(sid, payload)


def test_current_turn_telling_can_create_valid_source_before_use():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare(sid)

        scene = (
            "**Елена** — Завтра встречаюсь с Вейлом.\n"
            "**Лиам** — Кстати. Завтра ты к Вейлу во сколько?"
        )
        payload = _payload(
            scene,
            [
                {
                    "unit_id": "speech:1",
                    "character_id": "elena",
                    "speech_text": "Завтра встречаюсь с Вейлом.",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Елена завтра встречается с Вейлом.",
                        "source_fact_ids": ["elena_knows_own_veil_meeting"],
                    }],
                },
                {
                    "unit_id": "speech:2",
                    "character_id": "liam",
                    "speech_text": "Кстати. Завтра ты к Вейлу во сколько?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Елена завтра встречается с Вейлом.",
                        "source_event_ids": ["liam_hears_veil_meeting"],
                    }],
                },
            ],
            turn_knowledge=[
                {
                    "event_id": "liam_hears_veil_meeting",
                    "character_id": "liam",
                    "fact": "Елена сказала Лиаму, что завтра встречается с Вейлом.",
                    "source_kind": "told",
                    "evidence": "Завтра встречаюсь с Вейлом.",
                }
            ],
        )

        firewall._validate_knowledge_commit(sid, payload)
