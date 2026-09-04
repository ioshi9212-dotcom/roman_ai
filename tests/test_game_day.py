import json
import tempfile
from pathlib import Path

from app import storage
from app.game_day import sync_game_day
from app import session_runtime


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_game_day_is_derived_from_starting_date():
    source = {
        "starting_state": {
            "current": {"date": "02.09.2026"},
        }
    }
    assert sync_game_day({"current": {"date": "02.09.2026"}}, source)["current"]["game_day"] == 1
    assert sync_game_day({"current": {"date": "03.09.2026"}}, source)["current"]["game_day"] == 2
    assert sync_game_day({"current": {"date": "05.09.2026"}}, source)["current"]["game_day"] == 4


def test_prepare_turn_persists_correct_game_day_for_existing_session():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "game_day_test",
            "title": "Game Day Test",
            "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
            "starting_state": {
                "pov": {"character_id": "rina"},
                "current": {
                    "date": "02.09.2026",
                    "time": "10:00",
                    "location": "дом",
                    "present_characters": ["rina"],
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        state = storage._read_json(root / "state.json", {})
        state["current"]["date"] = "03.09.2026"
        storage._write_json(root / "state.json", state)

        manifest = session_runtime.prepare_turn_packet(sid, "Встать с кровати.")
        persisted = storage._read_json(root / "state.json", {})
        assert persisted["current"]["game_day"] == 2

        packet = storage._read_json(root / "turn_packet.json", {})
        context = json.loads("".join(packet["chunks"]))
        assert context["scene_state"]["current"]["game_day"] == 2
        assert manifest["prepared_for_turn"] == 1
