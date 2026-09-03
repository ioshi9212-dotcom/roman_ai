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


def read_packet(session_id: str, user_input: str):
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


def fresh_novel():
    return {
        "novel_id": "fresh_relationship",
        "title": "Fresh Relationship",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "room",
                "present_characters": ["rina", "adrian"],
            },
            "relationships": {},
        },
    }


def test_fresh_present_npc_is_evaluated_without_forced_baseline_and_real_footer_persists():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]

        first = read_packet(sid, "test")
        candidates = first["relationship_lens"]["present_npc_candidates"]
        adrian = next(item for item in candidates if item["character_id"] == "adrian")
        assert adrian["has_saved_relationship"] is False
        assert adrian["saved_dimensions"] == []
        assert first["relationship_lens"]["initialization_required"] is False
        assert "presence alone is not a reason" in adrian["initialization_rule"]
        relation = next(
            item
            for item in first["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        assert relation["dimensions"] == []
        assert first["relationship_policy"]["footer_required_for_every_present_npc"] is False
        assert first["relationship_policy"]["fresh_baseline_required"] is False
        assert first["relationship_policy"]["saved_dimensions_are_durable"] is True

        scene = """🎭 Fresh Relationship · осень

Сцена.

Состояние: спокойно
Отношения:
Эдриан - симпатия 12; настороженность 8

Ход 1 · цикл 1/15"""
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene,
                "extracted": extracted(),
            },
        )

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {
            "симпатия": 12,
            "настороженность": 8,
        }

        second = read_packet(sid, "next")
        relation = next(
            item
            for item in second["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        assert {(item["label"], item["value"]) for item in relation["dimensions"]} == {
            ("симпатия", 12),
            ("настороженность", 8),
        }


def test_empty_relationship_footer_for_fresh_present_npc_does_not_block_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]
        read_packet(sid, "test")

        scene = """🎭 Fresh Relationship · осень

Сцена без отношения, сформированного между персонажами.

Состояние: спокойно
Отношения:

Ход 1 · цикл 1/15"""
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene,
                "extracted": extracted(),
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state.get("relationships", {}).get("adrian") is None


def test_missing_cosmetic_row_preserves_existing_saved_relationship():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = fresh_novel()
        novel["starting_state"]["relationships"] = {
            "adrian": {"симпатия": 20, "доверие": 9}
        }
        sid = storage.create_session(novel)["session_id"]
        read_packet(sid, "test")

        scene = """🎭 Fresh Relationship · осень

Сцена.

Состояние: спокойно
Отношения:

Ход 1 · цикл 1/15"""
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene,
                "extracted": extracted(),
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 20, "доверие": 9}


def test_printed_saved_relationship_still_rejects_disappearing_dimensions():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = fresh_novel()
        novel["starting_state"]["relationships"] = {
            "adrian": {"симпатия": 20, "доверие": 9}
        }
        sid = storage.create_session(novel)["session_id"]
        read_packet(sid, "test")

        scene = """🎭 Fresh Relationship · осень

Сцена.

Состояние: спокойно
Отношения:
Эдриан - симпатия 21/+1

Ход 1 · цикл 1/15"""
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene,
                    "extracted": extracted(),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_DIMENSIONS_INCOMPLETE"
