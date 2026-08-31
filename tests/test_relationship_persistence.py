import json
import tempfile
from pathlib import Path

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


def test_relationship_footer_is_persisted_without_manual_state_patch():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "relationships",
            "title": "Relationships",
            "novel": {"pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Рината", "is_pov": True},
                {"character_id": "adrian", "name": "Эдриан"},
            ],
            "starting_state": {
                "pov": {"character_id": "rina"},
                "current": {"location": "room", "present_characters": ["rina", "adrian"]},
                "relationships": {"adrian": {"симпатия": 10, "близость": 5}},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        context = read_all_packet_chunks(sid, "test")
        assert context["relationship_policy"]["required_review_every_turn"] is True
        assert "Do not freeze" in context["relationship_policy"]["instruction"]

        scene = """🎭 Relationships · осень

Сцена.

Состояние: нормально
Отношения:
Эдриан - симпатия 12/+2; близость 7/+2

Ход 1 · цикл 1/15"""
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene,
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                },
            },
        )
        assert result["relationships_persisted_from_footer"] is True
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 12
        assert state["relationships"]["adrian"]["близость"] == 7


def test_absent_npc_footer_cannot_overwrite_relationship():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "relationships_absent",
            "title": "Relationships",
            "novel": {"pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Рината", "is_pov": True},
                {"character_id": "adrian", "name": "Эдриан"},
            ],
            "starting_state": {
                "pov": {"character_id": "rina"},
                "current": {"location": "room", "present_characters": ["rina"]},
                "relationships": {"adrian": {"симпатия": 10}},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        read_all_packet_chunks(sid, "test")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": "Состояние: нормально\nОтношения:\nЭдриан - симпатия 99/+89\n\nХод 1 · цикл 1/15",
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                },
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 10
