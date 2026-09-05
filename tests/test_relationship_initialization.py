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
    text = "".join(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], i)["content"] for i in range(manifest["chunk_count"]))
    return json.loads(text)


def extracted():
    return {"persistence_reviewed": True, "chronology": [], "knowledge_add": [], "experiences_add": [], "dialogue_memory_add": []}


def fresh_novel():
    return {
        "novel_id": "fresh_relationship",
        "title": "Fresh Relationship",
        "novel": {"pov_character": "rina"},
        "characters": [{"character_id": "rina", "name": "Рината", "is_pov": True}, {"character_id": "adrian", "name": "Эдриан"}],
        "starting_state": {"pov": {"character_id": "rina"}, "current": {"location": "room", "present_characters": ["rina", "adrian"]}, "relationships": {}},
    }


def test_fresh_present_npc_is_exposed_but_baseline_is_not_forced():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]
        first = read_packet(sid, "test")
        candidate = next(item for item in first["relationship_lens"]["present_npc_candidates"] if item["character_id"] == "adrian")
        assert candidate["has_saved_relationship"] is False
        assert candidate["saved_dimensions"] == []
        assert first["relationship_lens"]["initialization_required"] is False
        assert first["relationship_policy"]["footer_required_for_every_present_npc"] is False
        assert first["relationship_policy"]["fresh_baseline_required"] is False
        assert first["relationship_policy"]["new_dimensions_may_be_appended"] is True


def test_fresh_relationship_may_stay_empty_until_story_creates_one():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]
        read_packet(sid, "test")
        scene = "🎭 Fresh Relationship · осень\n\nСцена.\n\nСостояние: спокойно\nОтношения:\n\nХод 1 · цикл 1/15"
        session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene, "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state.get("relationships", {}).get("adrian", {}) == {}


def test_first_meaningful_footer_persists_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(fresh_novel())["session_id"]
        read_packet(sid, "test")
        scene = "🎭 Fresh Relationship · осень\n\nСцена.\n\nСостояние: спокойно\nОтношения:\nЭдриан - симпатия 12; настороженность 8\n\nХод 1 · цикл 1/15"
        session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene, "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 12, "настороженность": 8}


def test_commit_rejects_disappearing_nonzero_saved_dimensions():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = fresh_novel()
        novel["starting_state"]["relationships"] = {"adrian": {"симпатия": 20, "доверие": 9}}
        sid = storage.create_session(novel)["session_id"]
        read_packet(sid, "test")
        scene = "🎭 Fresh Relationship · осень\n\nСцена.\n\nСостояние: спокойно\nОтношения:\nЭдриан - симпатия 21/+1\n\nХод 1 · цикл 1/15"
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene, "extracted": extracted()})
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_DIMENSIONS_INCOMPLETE"
