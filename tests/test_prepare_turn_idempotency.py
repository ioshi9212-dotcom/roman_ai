import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage
from app.main import turn_packet_prepare
from app.models import TurnPrepare


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


def _commit_one_turn(sid: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    return session_runtime.commit_turn(
        sid,
        {
            "packet_id": manifest["packet_id"],
            "user_input": user_input,
            "scene_output": (
                "🎭 Duplicate guard · осень\n"
                "🕒 День 1 · вторник, 01.09.2026, 10:01 · 📍 room\n\n"
                "Сохранённая сцена.\n\nСостояние: спокойно\nОтношения:\n\nХод 1"
            ),
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
                "npc_intent_updates": [],
                "story_thread_updates": [],
            },
        },
    )


def test_recent_exact_committed_input_is_replayed_instead_of_creating_new_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "Один и тот же пользовательский ход."
        _commit_one_turn(sid, text)

        root = storage.SESSIONS_DIR / sid
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1

        response = turn_packet_prepare(sid, TurnPrepare(user_input=text))
        assert response["already_committed_duplicate"] is True
        assert response["duplicate_guard"] is True
        assert response["turn_number"] == 1
        assert response["scene_output"].startswith("🎭 Duplicate guard")
        assert not (root / "turn_packet.json").exists()
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1


def test_commit_boundary_rejects_recent_duplicate_even_if_internal_prepare_is_called_directly():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "Повторить дословно."
        _commit_one_turn(sid, text)

        # Bypass the public prepare endpoint to model a race or stale internal caller.
        manifest = session_runtime.prepare_turn_packet(sid, text)
        start = 1 if manifest.get("first_chunk_included") else 0
        for index in range(start, manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)

        with pytest.raises(RuntimeError, match="RECENT_DUPLICATE_USER_INPUT"):
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": text,
                    "scene_output": (
                        "🎭 Duplicate guard · осень\n"
                        "🕒 День 1 · вторник, 01.09.2026, 10:02 · 📍 room\n\n"
                        "Эта сцена не должна сохраниться.\n\nСостояние: спокойно\nОтношения:\n\nХод 2"
                    ),
                    "extracted": {
                        "persistence_reviewed": True,
                        "chronology": [],
                        "knowledge_add": [],
                        "experiences_add": [],
                        "dialogue_memory_add": [],
                        "npc_intent_updates": [],
                        "story_thread_updates": [],
                    },
                },
            )

        assert storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})["turn_number"] == 1
        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 1
