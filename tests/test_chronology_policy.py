import json
import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "chrono",
        "title": "Chronology",
        "version": 1,
        "novel": {"pov_character": "emily"},
        "characters": [
            {"character_id": "emily", "name": "Эмили", "is_pov": True},
            {"character_id": "kai", "name": "Кай"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "emily"},
            "current": {
                "date": "03.09.2026",
                "time": "09:18",
                "location": "кофейня",
                "present_characters": ["emily", "kai"],
            },
        },
    }


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    text = ""
    for index in range(manifest["chunk_count"]):
        text += storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"]
    return manifest, json.loads(text)


def reviewed(**extra):
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }
    value.update(extra)
    return value


def with_kai_relationship(text: str, metrics: str = "настороженность 5") -> str:
    return f"{text}\n\nСостояние: спокойно\nОтношения:\nКай - {metrics}\n\nХод 1 · цикл 1/15"


def test_empty_extracted_is_rejected_instead_of_silently_losing_memory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "Кай?")
        with pytest.raises(RuntimeError, match="PERSISTENCE_REVIEW_REQUIRED"):
            session_runtime.commit_turn(
                sid,
                {"user_input": "Кай?", "scene_output": "Они познакомились.", "extracted": {}},
            )


def test_routine_turn_can_explicitly_save_no_chronology():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "(допить кофе)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(допить кофе)",
                "scene_output": with_kai_relationship("Эмили допила кофе и вернулась к работе."),
                "extracted": reviewed(),
            },
        )
        assert result["saved_chronology_events"] == 0
        assert storage._read_json(storage.SESSIONS_DIR / sid / "chronology.json", []) == []


def test_chronology_uses_period_and_drops_unimportant_exact_time():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "Поздно. Эмили.")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "Поздно. Эмили.",
                "scene_output": with_kai_relationship(
                    "Эмили представилась Каю. Они поговорили о мотоциклах.",
                    "симпатия 8; настороженность 4",
                ),
                "extracted": reviewed(
                    chronology=[
                        {
                            "event": "Эмили и Кай лично познакомились: Эмили назвала своё имя; они коротко поговорили о мотоциклах.",
                            "importance": "anchor",
                            "exact_time": "09:18",
                            "participants_present": ["Эмили", "Кай"],
                        }
                    ],
                    knowledge_add=[
                        {"character_id": "emily", "content": "Мужчину зовут Кай"},
                        {"character_id": "kai", "content": "Девушку зовут Эмили"},
                    ],
                ),
            },
        )
        event = storage._read_json(storage.SESSIONS_DIR / sid / "chronology.json", [])[0]
        assert event["story_date"] == "03.09.2026"
        assert event["period"] == "утро"
        assert "exact_time" not in event
        assert event["participants_present"] == ["emily", "kai"]
        assert event["importance"] == "anchor"

        memory = storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        assert memory["characters"]["emily"]["knowledge"][0]["fact_id"].startswith("fact_t1_")
        assert memory["characters"]["kai"]["knowledge"][0]["fact_id"].startswith("fact_t1_")


def test_exact_time_is_kept_only_when_marked_time_critical():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "В девять восемнадцать.")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "В девять восемнадцать.",
                "scene_output": with_kai_relationship(
                    "Они договорились встретиться ровно в 09:18.",
                    "доверие 6; настороженность 3",
                ),
                "extracted": reviewed(
                    chronology=[
                        {
                            "event": "Эмили и Кай договорились встретиться в точно назначенное время.",
                            "importance": "major",
                            "time_critical": True,
                            "exact_time": "09:18",
                        }
                    ]
                ),
            },
        )
        event = storage._read_json(storage.SESSIONS_DIR / sid / "chronology.json", [])[0]
        assert event["exact_time"] == "09:18"
        assert event["time_critical"] is True


def test_old_anchor_remains_in_packet_after_hundreds_of_events():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        chronology = [
            {
                "event_id": "first_meeting",
                "turn_number": 1,
                "story_date": "03.09.2026",
                "period": "утро",
                "location": "кофейня",
                "participants_present": ["emily", "kai"],
                "event": "Эмили и Кай впервые лично познакомились и узнали имена друг друга.",
                "importance": "anchor",
            }
        ]
        for turn in range(2, 302):
            chronology.append(
                {
                    "event_id": f"event_{turn}",
                    "turn_number": turn,
                    "event": f"Событие {turn}",
                    "importance": "normal",
                    "participants_present": ["emily"],
                }
            )
        storage._write_json(root / "chronology.json", chronology)
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 301
        meta["last_audit_turn"] = 300
        meta["audit_required"] = False
        storage._write_json(root / "meta.json", meta)

        _, packet = read_packet(sid, "Кай уже приходил сюда раньше.")
        ids = {event.get("event_id") for event in packet["chronology_recent"]}
        assert "first_meeting" in ids
        assert "event_301" in ids
        assert len(packet["chronology_recent"]) < 50
        assert "persistence_contract" in packet
        assert "chronology_policy" in packet
