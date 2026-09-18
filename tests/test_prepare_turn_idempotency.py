import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import storage
from app.models import TurnPrepare
from app.operation_service import commit_turn_request, prepare_turn_request


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


def _read_packet(sid: str, manifest: dict):
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)


def _commit_one_turn(sid: str, user_input: str, request_id: str, minute: int = 1):
    manifest = prepare_turn_request(sid, user_input, request_id)
    assert manifest.get("already_committed_duplicate") is not True
    _read_packet(sid, manifest)
    return commit_turn_request(
        sid,
        {
            "packet_id": manifest["packet_id"],
            "user_input": user_input,
            "scene_output": (
                "🎭 Duplicate guard · осень\n"
                f"🕒 День 1 · вторник, 01.09.2026, 10:{minute:02d} · 📍 room\n\n"
                f"Сохранённая сцена {minute}.\n\nСостояние: спокойно\nОтношения:\n\nХод {minute}"
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


def test_same_request_id_reuses_pending_packet_and_keeps_read_progress():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = prepare_turn_request(sid, "Тот же ход.", "req-pending-1")
        storage.get_turn_packet_chunk(sid, first["packet_id"], 0)

        second = prepare_turn_request(sid, "Тот же ход.", "req-pending-1")
        assert second["packet_id"] == first["packet_id"]
        assert second["request_id"] == "req-pending-1"
        assert second["prepared_for_turn"] == first["prepared_for_turn"] == 1
        assert second["reused_pending_packet"] is True

        saved = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert saved["packet_id"] == first["packet_id"]
        assert saved["request_id"] == "req-pending-1"
        assert 0 in saved["read_chunks"]


def test_different_request_id_replaces_pending_packet_even_when_text_is_identical():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = prepare_turn_request(sid, "Одинаковый текст.", "req-a")
        second = prepare_turn_request(sid, "Одинаковый текст.", "req-b")

        assert second["packet_id"] != first["packet_id"]
        assert second["request_id"] == "req-b"
        assert second["prepared_for_turn"] == 1
        with pytest.raises(PermissionError):
            storage.get_turn_packet_chunk(sid, first["packet_id"], 0)


def test_same_request_id_with_changed_input_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        prepare_turn_request(sid, "Первый текст.", "req-fixed")

        with pytest.raises(RuntimeError, match="TURN_REQUEST_ID_REUSED"):
            prepare_turn_request(sid, "Другой текст.", "req-fixed")


def test_committed_request_id_replays_saved_scene_after_response_loss():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "Один пользовательский ход."
        _commit_one_turn(sid, text, "req-committed", 1)

        root = storage.SESSIONS_DIR / sid
        response = prepare_turn_request(sid, text, "req-committed")

        assert response["already_committed_duplicate"] is True
        assert response["duplicate_guard"] == "request_id"
        assert response["request_id"] == "req-committed"
        assert response["turn_number"] == 1
        assert response["scene_output"].startswith("🎭 Duplicate guard")
        assert not (root / "turn_packet.json").exists()
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1


def test_identical_text_with_new_request_id_creates_a_real_second_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "Ещё раз."

        first = _commit_one_turn(sid, text, "req-repeat-1", 1)
        second = _commit_one_turn(sid, text, "req-repeat-2", 2)

        root = storage.SESSIONS_DIR / sid
        turns = storage._read_turns(root)
        assert first["turn_number"] == 1
        assert second["turn_number"] == 2
        assert [turn["user_input"] for turn in turns] == [text, text]
        assert [turn["request_id"] for turn in turns] == ["req-repeat-1", "req-repeat-2"]


def test_committed_request_id_cannot_be_reused_with_different_input():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        _commit_one_turn(sid, "Исходный текст.", "req-locked", 1)

        with pytest.raises(RuntimeError, match="TURN_REQUEST_ID_REUSED"):
            prepare_turn_request(sid, "Теперь другой текст.", "req-locked")

        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 1


def test_public_prepare_model_requires_request_id():
    with pytest.raises(ValidationError):
        TurnPrepare(user_input="Продолжить.")

    value = TurnPrepare(user_input="Продолжить.", request_id="req-model")
    assert value.request_id == "req-model"
