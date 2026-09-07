import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel(starting_relationships=None):
    return {
        "novel_id": "relationship_test",
        "title": "Relationship Test",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "дом", "scene": "кухня", "present_characters": ["rina", "adrian"]},
            "relationships": starting_relationships or {},
        },
    }


def read_all_packet_chunks(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if parts else 0
    for index in range(start, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def scene(metrics: str, turn: int = 1) -> str:
    return f"""🎭 Relationship Test · осень
🕒 День 1 · 07.09.2026, 12:00 · 📍 дом

Сцена.

Состояние: нормально
Отношения:
Эдриан - {metrics}

Ход {turn} · цикл {turn}/15"""


def test_flat_relationships_migrate_to_old_generator_documents():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        context = read_all_packet_chunks(sid, "test")
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert "relationship_documents" in state
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["target_character_id"] == "rina"
        assert {(item["label"], item["value"]) for item in relation["dimensions"]} == {
            ("симпатия", 10),
            ("близость", 5),
        }
        assert "relationship_schemas" not in state
        assert context["writer_contract"]
        assert "relationship_contract" not in context
        assert "runtime_documents" not in context
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {
            "симпатия": 10,
            "близость": 5,
        }


def test_footer_persists_changes_and_accepts_missing_delta_like_old_generator():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        read_all_packet_chunks(sid, "test")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("симпатия 12; близость 5"),
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                },
            },
        )
        assert result["ok"] is True
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        values = {item["label"]: item["value"] for item in relation["dimensions"]}
        assert values == {"симпатия": 12, "близость": 5}
        assert state["relationships"]["adrian"] == {"симпатия": 12, "близость": 5}


def test_new_dimension_can_be_added_without_erasing_old_dimensions():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}})
        )["session_id"]
        read_all_packet_chunks(sid, "test")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("симпатия 10; близость 5; раздражение 3"),
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
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        values = {item["label"]: item["value"] for item in relation["dimensions"]}
        assert values == {"симпатия": 10, "близость": 5, "раздражение": 3}
