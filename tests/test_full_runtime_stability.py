import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel_fixture():
    return {
        "novel_id": "stability_test",
        "title": "Stability",
        "novel": {"title": "Stability"},
        "characters": [
            {"character_id": "pov", "name": "Рина", "is_pov": True},
            {"character_id": "present", "name": "Эдриан"},
            {"character_id": "dormant", "name": "Далёкий NPC", "secret": "must stay stored"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "02.09.2026",
                "time": "23:50",
                "location": "дом",
                "present_characters": ["pov", "present"],
            },
        },
        "lore": {"large_canon": "canon stays in Railway"},
    }


def read_packet(root: Path):
    packet = storage._read_json(root / "turn_packet.json", {})
    return packet, json.loads("".join(packet["chunks"]))


def mark_packet_read(root: Path):
    packet = storage._read_json(root / "turn_packet.json", {})
    packet["read_chunks"] = list(range(packet["chunk_count"]))
    storage._write_json(root / "turn_packet.json", packet)


def test_turn_packet_keeps_dormant_character_persisted_but_out_of_working_context():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest = session_runtime.prepare_turn_packet(sid, "Посмотреть на Эдриана.")
        _packet, context = read_packet(root)

        assert manifest["working_context"] is True
        ids = {row["character_id"] for row in context["scene_character_cards"]}
        assert {"pov", "present"}.issubset(ids)
        assert "dormant" not in ids
        assert "dormant" in {row["character_id"] for row in context["character_registry_index"]}

        persisted = storage._read_json(root / "characters.json", [])
        dormant = next(row for row in persisted if row["character_id"] == "dormant")
        assert dormant["secret"] == "must stay stored"


def test_commit_updates_story_clock_from_scene_header_and_persists_all_files():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        user_input = "Дождаться полуночи."
        session_runtime.prepare_turn_packet(sid, user_input)
        mark_packet_read(root)

        scene = (
            "🎭 Stability · осень\n"
            "🕒 День 2 · четверг, 03.09.2026, 00:05 · 📍 кухня\n"
            "🌦️ Погода: ясно\n"
            "⚙️ Сцена: тест\n"
            "----------------------------------------\n"
            "Тестовая сцена.\n\n"
            "Отношения:\n"
            "Эдриан - доверие 10\n"
        )
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": scene,
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [{"event": "Наступили следующие сутки."}],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "state_patch": {
                        "current": {"present_characters": ["pov", "present"]}
                    },
                },
            },
        )

        assert result["turn_number"] == 1
        state = storage._read_json(root / "state.json", {})
        assert state["current"]["date"] == "03.09.2026"
        assert state["current"]["time"] == "00:05"
        assert state["current"]["location"] == "кухня"
        assert state["current"]["game_day"] == 2
        assert state["characters"]["present"]["location"] == "кухня"
        assert len(storage._read_turns(root)) == 1
        assert storage._read_json(root / "chronology.json", [])
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1
