import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def make_session() -> str:
    novel = {
        "novel_id": "idempotent_prepare",
        "title": "Idempotent Prepare",
        "novel": {"pov_character": "pov"},
        "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["pov"],
            },
        },
    }
    return storage.create_session(novel)["session_id"]


def test_same_pending_prepare_reuses_packet_and_keeps_read_progress():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = session_runtime.prepare_turn_packet(sid, "Тот же ход.")
        storage.get_turn_packet_chunk(sid, first["packet_id"], 0)

        second = session_runtime.prepare_turn_packet(sid, "Тот же ход.")
        assert second["packet_id"] == first["packet_id"]
        assert second["prepared_for_turn"] == first["prepared_for_turn"] == 1
        assert second["reused_pending_packet"] is True
        assert 0 in second["read_chunks"]

        saved = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert saved["packet_id"] == first["packet_id"]
        assert 0 in saved["read_chunks"]


def test_different_pending_input_replaces_packet_but_old_id_becomes_stale():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = session_runtime.prepare_turn_packet(sid, "Первый вариант.")
        second = session_runtime.prepare_turn_packet(sid, "Исправленный вариант.")

        assert second["packet_id"] != first["packet_id"]
        assert second["prepared_for_turn"] == 1
        assert second["reused_pending_packet"] is False
        with pytest.raises(PermissionError):
            storage.get_turn_packet_chunk(sid, first["packet_id"], 0)
