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


def test_fresh_present_npc_is_exposed_for_relationship_initialization_and_persists_first_footer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]

        first = read_packet(sid, "test")
        candidates = first["relationship_lens"]["present_npc_candidates"]
        adrian = next(item for item in candidates if item["character_id"] == "adrian")
        assert adrian["has_saved_relationship"] is False
        assert adrian["saved_dimensions"] == []
        assert first["relationship_lens"]["initialization_required"] is True
        assert "do not output an empty relationship block" in first["relationship_lens_instruction"]
        relation = next(
            item
            for item in first["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        assert relation["dimensions"] == []
        assert first["relationship_policy"]["footer_required_for_every_present_npc"] is True
        assert "metric_names_locked" not in first["relationship_policy"]
        assert "strict arithmetic" not in first["relationship_policy"]["instruction"]

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
        candidate = next(
            item
            for item in second["relationship_lens"]["present_npc_candidates"]
            if item["character_id"] == "adrian"
        )
        assert candidate["has_saved_relationship"] is True


def test_commit_rejects_empty_relationship_footer_when_npc_is_present():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]
        read_packet(sid, "test")

        scene = """🎭 Fresh Relationship · осень

Сцена.

Состояние: спокойно
Отношения:

Ход 1 · цикл 1/15"""
        with pytest.raises(RuntimeError, match="RELATIONSHIP_FOOTER_REQUIRED"):
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene,
                    "extracted": extracted(),
                },
            )


def test_commit_rejects_disappearing_saved_dimensions():
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
        with pytest.raises(RuntimeError, match="RELATIONSHIP_FOOTER_INCOMPLETE"):
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene,
                    "extracted": extracted(),
                },
            )
