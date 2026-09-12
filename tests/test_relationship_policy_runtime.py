import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "relationship_policy",
        "title": "Relationship Policy",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "liam", "name": "Лиам"},
            {"character_id": "aiden", "name": "Эйден"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "room", "present_characters": ["rina", "liam"]},
            "relationships": {
                "liam": {"симпатия": 1, "доверие": 2, "привязанность": 10},
                "aiden": {"симпатия": 4, "настороженность": 3},
            },
        },
    }


def extracted(**extra):
    result = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
        "presence_updates": [],
        "relationship_updates": [],
        "state_patch": {},
        "character_upserts": [],
    }
    result.update(extra)
    return result


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if parts else 0
    for index in range(start, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(parts))


def scene(metrics: str, turn: int = 1):
    return f"""🎭 Relationship Policy · осень

Сцена.

Состояние: спокойно
Отношения:
Лиам - {metrics}

Ход {turn} · цикл {turn}/15"""


def test_relationship_index_always_contains_offscreen_npcs():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        _, packet = read_packet(sid, "обычный ход")

        index = packet["relationship_index"]
        assert index["always_read"] is True
        assert index["direction"] == "NPC -> POV"
        assert index["characters"]["liam"]["metrics"]["симпатия"] == 1
        assert index["characters"]["aiden"]["metrics"]["настороженность"] == 3


def test_established_metric_cannot_silently_disappear_from_footer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "дальше")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "дальше",
                    "scene_output": scene("симпатия 1; привязанность 10"),
                    "extracted": extracted(),
                },
            )

        assert exc.value.detail["code"] == "RELATIONSHIP_FOOTER_INCOMPLETE"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["liam"]["доверие"] == 2


def test_ordinary_change_requires_reason_and_persists_reason():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "Лиам видит, что Рината сдержала обещание")

        update = {
            "character_id": "liam",
            "reason": "Рината сдержала данное Лиаму обещание, и для него надёжность важна.",
            "change_scale": "ordinary",
            "dimensions": [{"label": "доверие", "value": 3, "delta": 1}],
        }
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "Лиам видит, что Рината сдержала обещание",
                "scene_output": scene("симпатия 1; доверие 3/+1; привязанность 10"),
                "extracted": extracted(relationship_updates=[update]),
            },
        )

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["liam"]["доверие"] == 3
        relation = state["relationship_documents"]["liam"]["relations"][0]
        reason = relation["change_reasons"][-1]
        assert reason["turn"] == 1
        assert "сдержала" in reason["reason"]
        assert reason["changes"][0]["delta"] == 1


def test_ordinary_change_without_reason_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "дальше")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "дальше",
                    "scene_output": scene("симпатия 2/+1; доверие 2; привязанность 10"),
                    "extracted": extracted(
                        relationship_updates=[
                            {
                                "character_id": "liam",
                                "dimensions": [{"label": "симпатия", "value": 2, "delta": 1}],
                            }
                        ]
                    ),
                },
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_CHANGE_REASON_REQUIRED"


def test_ordinary_change_over_three_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "важный разговор")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "важный разговор",
                    "scene_output": scene("симпатия 5/+4; доверие 2; привязанность 10"),
                    "extracted": extracted(
                        relationship_updates=[
                            {
                                "character_id": "liam",
                                "reason": "Хороший разговор.",
                                "change_scale": "ordinary",
                                "dimensions": [{"label": "симпатия", "value": 5, "delta": 4}],
                            }
                        ]
                    ),
                },
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_DELTA_OUT_OF_RANGE"


def test_five_day_timeskip_can_accumulate_large_change():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "(пропустить 5 дней, проводить с Лиамом почти всё свободное время)")

        update = {
            "character_id": "liam",
            "reason": "Пять дней регулярного близкого общения и совместного свободного времени.",
            "change_scale": "timeskip",
            "elapsed_game_days": 5,
            "dimensions": [{"label": "привязанность", "value": 20, "delta": 10}],
        }
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "(пропустить 5 дней, проводить с Лиамом почти всё свободное время)",
                "scene_output": scene("симпатия 1; доверие 2; привязанность 20/+10"),
                "extracted": extracted(relationship_updates=[update]),
            },
        )

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["liam"]["привязанность"] == 20


def test_absent_name_mention_is_not_participation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "вспомнить Эйдена")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "вспомнить Эйдена",
                    "scene_output": scene("симпатия 1; доверие 2; привязанность 10"),
                    "extracted": extracted(
                        relationship_updates=[
                            {
                                "character_id": "aiden",
                                "reason": "Просто вспомнили его.",
                                "change_scale": "ordinary",
                                "dimensions": [{"label": "симпатия", "value": 5, "delta": 1}],
                            }
                        ]
                    ),
                },
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC"


def test_remote_dialogue_counts_as_real_participation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "написать Эйдену и поговорить")

        update = {
            "character_id": "aiden",
            "reason": "В прямой переписке Рината ответила на вопрос, который Эйден считал важным.",
            "change_scale": "ordinary",
            "dimensions": [{"label": "настороженность", "value": 2, "delta": -1}],
        }
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "написать Эйдену и поговорить",
                "scene_output": scene("симпатия 1; доверие 2; привязанность 10"),
                "extracted": extracted(
                    dialogue_memory_add=[
                        {
                            "participants": ["rina", "aiden"],
                            "summary": "Рината и Эйден поговорили в сообщениях.",
                        }
                    ],
                    relationship_updates=[update],
                ),
            },
        )

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["aiden"]["настороженность"] == 2
