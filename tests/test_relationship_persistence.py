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


def read_all_packet_chunks(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    text = "".join(
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"]
        for index in range(manifest["chunk_count"])
    )
    return json.loads(text)


def extracted():
    return {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }


def novel(present=True, starting_relationships=None):
    return {
        "novel_id": "relationships",
        "title": "Relationships",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "room",
                "present_characters": ["rina", "adrian"] if present else ["rina"],
            },
            "relationships": starting_relationships or {},
        },
    }


def scene(metrics: str, turn=1):
    return f"""🎭 Relationships · осень

Сцена.

Состояние: нормально
Отношения:
Эдриан - {metrics}

Ход {turn} · цикл {turn}/15"""


def test_relationship_footer_is_persisted_without_manual_state_patch():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        context = read_all_packet_chunks(sid, "test")
        assert context["relationship_policy"]["required_review_every_turn"] is True
        assert context["relationship_policy"]["metric_names_locked"] is True
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {
            "симпатия": 10,
            "близость": 5,
        }
        assert "DO NOT rename" in context["relationship_policy"]["instruction"]
        assert "FINAL VALUE = saved start value + displayed delta" in context["relationship_policy"]["instruction"]

        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("симпатия 12/+2; близость 7/+2"),
                "extracted": extracted(),
            },
        )
        assert result["relationships_persisted_from_footer"] is True
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 12, "близость": 7}
        assert state["relationship_schemas"]["adrian"] == ["симпатия", "близость"]


def test_relationship_metric_names_cannot_change_on_reunion():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        read_all_packet_chunks(sid, "test")
        with pytest.raises(RuntimeError, match="RELATIONSHIP_SCHEMA_MISMATCH:adrian"):
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene("влечение 12/+2; доверие 7/+2"),
                    "extracted": extracted(),
                },
            )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 10, "близость": 5}


def test_relationship_final_value_must_equal_saved_value_plus_delta():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        read_all_packet_chunks(sid, "test")
        with pytest.raises(RuntimeError, match="RELATIONSHIP_ARITHMETIC_MISMATCH:adrian:симпатия"):
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene("симпатия 50/+2; близость 7/+2"),
                    "extracted": extracted(),
                },
            )


def test_first_footer_establishes_schema_then_it_is_sticky():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_all_packet_chunks(sid, "first")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "first",
                "scene_output": scene("интерес 8/+1; доверие 3/+1"),
                "extracted": extracted(),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationship_schemas"]["adrian"] == ["интерес", "доверие"]
        assert state["relationships"]["adrian"] == {"интерес": 8, "доверие": 3}

        read_all_packet_chunks(sid, "second")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "second",
                "scene_output": scene("интерес 10/+2; доверие 3/+0", turn=2),
                "extracted": extracted(),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"интерес": 10, "доверие": 3}


def test_prepare_turn_repairs_old_renamed_metrics_from_turn_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        turns = [
            {
                "turn_number": 1,
                "scene_output": scene("симпатия 10/+1; близость 5/+1", turn=1),
            },
            {
                "turn_number": 2,
                "scene_output": scene("симпатия 12/+2; близость 6/+1", turn=2),
            },
            {
                "turn_number": 3,
                "scene_output": scene("влечение 77/+65; доверие 91/+85", turn=3),
            },
        ]
        with (root / "turns.jsonl").open("w", encoding="utf-8") as fh:
            for turn in turns:
                fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        broken_state = storage._read_json(root / "state.json", {})
        broken_state["relationships"] = {
            "adrian": {
                "симпатия": 12,
                "близость": 6,
                "влечение": 77,
                "доверие": 91,
            }
        }
        storage._write_json(root / "state.json", broken_state)

        context = read_all_packet_chunks(sid, "reunion")
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"] == {
            "metrics": {"симпатия": 12, "близость": 6},
            "schema": ["симпатия", "близость"],
        }
        repaired = storage._read_json(root / "state.json", {})
        assert repaired["relationships"]["adrian"] == {"симпатия": 12, "близость": 6}
        assert repaired["relationship_schemas"]["adrian"] == ["симпатия", "близость"]


def test_relationship_key_by_character_name_is_canonicalized_to_id():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"Эдриан": {"симпатия": 10}})
        )["session_id"]
        context = read_all_packet_chunks(sid, "test")
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {
            "симпатия": 10
        }
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "Эдриан" not in state["relationships"]
        assert state["relationships"]["adrian"] == {"симпатия": 10}


def test_absent_npc_footer_cannot_overwrite_relationship():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(present=False, starting_relationships={"adrian": {"симпатия": 10}})
        )["session_id"]
        read_all_packet_chunks(sid, "test")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("симпатия 99/+89"),
                "extracted": extracted(),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 10
