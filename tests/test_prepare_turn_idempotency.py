import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage
from app.main import turn_packet_prepare
from app.models import TurnPrepare
from app.operation_service import prepare_turn_request
from app.runtime_access import runtime_documents


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



def test_new_turn_serializes_one_final_packet_without_duplicate_writer_context(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        writes = []
        original_write = storage._write_json

        def traced_write(path, data):
            if path.name == "turn_packet.json":
                writes.append(path)
            return original_write(path, data)

        monkeypatch.setattr(storage, "_write_json", traced_write)
        manifest = session_runtime.prepare_turn_packet(sid, "Проверка одного прохода.")

        assert len(writes) == 1
        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        context = json.loads("".join(packet["chunks"]))
        documents = runtime_documents()

        # Author runtime files remain complete and unchanged.
        assert context["runtime_rules"] == documents["rules"]
        assert context["scene_builder"] == documents["scene_builder"]

        # One canonical representation per concept.
        assert "character_profiles" not in context
        assert "character_registry" not in context
        assert "scene_characters" not in context
        assert context["character_cards"][0]["name"] == "POV"
        assert context["cast_registry"]["registry_index_path"] == "cast_registry.characters"
        assert manifest["chunk_chars_max"] == 16000

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



def _commit_request_turn(sid: str, user_input: str, request_id: str, turn: int):
    manifest = prepare_turn_request(sid, user_input, request_id)
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
                f"🕒 День 1 · вторник, 01.09.2026, 10:{turn:02d} · 📍 room\n\n"
                f"Сохранённая сцена {turn} без событий.\n\nСостояние: спокойно\nОтношения:\n\nХод {turn}"
            ),
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
                "npc_intent_updates": [],
                "story_thread_updates": [],
                "scene_progressed": False,
            },
        },
    )


def test_request_id_allows_identical_text_as_two_real_gameplay_turns():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "(Ещё раз без событий.)"

        first = _commit_request_turn(sid, text, "req-repeat-1", 1)
        second = _commit_request_turn(sid, text, "req-repeat-2", 2)

        turns = storage._read_turns(storage.SESSIONS_DIR / sid)
        assert first["turn_number"] == 1
        assert second["turn_number"] == 2
        assert [row["user_input"] for row in turns] == [text, text]
        assert [row["request_id"] for row in turns] == ["req-repeat-1", "req-repeat-2"]


def test_same_committed_request_id_replays_saved_scene_without_new_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        text = "(Один сетевой запрос без событий.)"
        _commit_request_turn(sid, text, "req-retry", 1)

        response = prepare_turn_request(sid, text, "req-retry")
        assert response["already_committed_duplicate"] is True
        assert response["duplicate_guard"] == "request_id"
        assert response["request_id"] == "req-retry"
        assert response["turn_number"] == 1
        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 1


def test_new_request_cannot_silently_replace_an_uncommitted_pending_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = turn_packet_prepare(
            sid,
            TurnPrepare(user_input="Первый незаписанный ход.", request_id="req-pending-1"),
        )

        with pytest.raises(HTTPException) as exc:
            turn_packet_prepare(
                sid,
                TurnPrepare(user_input="Другой ход.", request_id="req-pending-2"),
            )

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "TURN_IN_PROGRESS"
        pending = exc.value.detail["pending_turn"]
        assert pending["packet_id"] == first["packet_id"]
        assert pending["request_id"] == "req-pending-1"
        assert pending["user_input"] == "Первый незаписанный ход."

        saved = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert saved["packet_id"] == first["packet_id"]


def test_explicit_same_input_pending_rebuild_replaces_only_uncommitted_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = prepare_turn_request(
            sid,
            "Тот же зависший ход.",
            "req-stuck",
        )
        root = storage.SESSIONS_DIR / sid
        meta_before = storage._read_json(root / "meta.json", {})
        assert meta_before["turn_number"] == 0

        rebuilt = prepare_turn_request(
            sid,
            "Тот же зависший ход.",
            "req-stuck",
            replace_pending=True,
        )

        assert rebuilt["packet_id"] != first["packet_id"]
        assert rebuilt["prepared_for_turn"] == 1
        assert rebuilt["request_id"] == "req-stuck"
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 0

        abandoned = storage._read_json(root / "abandoned_turn_packets.json", [])
        assert abandoned[-1]["packet_id"] == first["packet_id"]
        assert abandoned[-1]["request_id"] == "req-stuck"
        assert abandoned[-1]["user_input"] == "Тот же зависший ход."
        assert abandoned[-1]["reason"] == "explicit_rebuild_pending"


def test_explicit_pending_replacement_preserves_diagnostic_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        first = prepare_turn_request(sid, "Старый незаписанный ход.", "req-old")
        second = prepare_turn_request(
            sid,
            "Новый ход.",
            "req-new",
            replace_pending=True,
        )

        assert second["packet_id"] != first["packet_id"]
        root = storage.SESSIONS_DIR / sid
        abandoned = storage._read_json(root / "abandoned_turn_packets.json", [])
        assert abandoned[-1]["packet_id"] == first["packet_id"]
        assert abandoned[-1]["request_id"] == "req-old"
        assert abandoned[-1]["user_input"] == "Старый незаписанный ход."
        assert abandoned[-1]["reason"] == "explicit_replace_pending"


def test_prepare_schema_stays_small_without_review_capabilities():
    value = TurnPrepare(user_input="Продолжить.")
    assert value.request_id is None
    assert value.scene_archive_capable is False
    assert value.replace_pending is False
    dumped = value.model_dump()
    assert "complete_knowledge_read_capable" not in dumped
    assert "runtime_contract_capable" not in dumped
    assert "strict_knowledge_capable" not in dumped



def test_retry_of_old_committed_request_does_not_disturb_newer_pending_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        old_text = "(Уже сохранённый ход без событий.)"
        _commit_request_turn(sid, old_text, "req-old-committed", 1)

        pending = prepare_turn_request(sid, "Новый незаписанный ход.", "req-new-pending")
        root = storage.SESSIONS_DIR / sid
        before = storage._read_json(root / "turn_packet.json", {})

        replay = prepare_turn_request(sid, old_text, "req-old-committed")

        assert replay["already_committed_duplicate"] is True
        assert replay["request_id"] == "req-old-committed"
        after = storage._read_json(root / "turn_packet.json", {})
        assert after == before
        assert after["packet_id"] == pending["packet_id"]
        assert after["request_id"] == "req-new-pending"
