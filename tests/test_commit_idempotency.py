import tempfile
from pathlib import Path

import pytest

from app import audit_runtime, session_runtime, storage
from app.operation_receipts import OperationReceiptConflict, RECEIPTS_FILE
from app.operation_service import (
    commit_audit_request,
    commit_turn_request,
    rollback_last_turn_request,
)


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel_fixture():
    return {
        "novel_id": "idempotency_test",
        "title": "Idempotency",
        "novel": {"title": "Idempotency"},
        "characters": [
            {"character_id": "pov", "name": "Рина", "is_pov": True},
            {"character_id": "present", "name": "Эдриан"},
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
        "lore": {},
    }


def valid_payload(user_input: str = "Дождаться полуночи."):
    scene = (
        "🎭 Idempotency · осень\n"
        "🕒 День 2 · четверг, 03.09.2026, 00:05 · 📍 кухня\n"
        "🌦️ Погода: ясно\n"
        "⚙️ Сцена: тест\n"
        "----------------------------------------\n"
        "Тестовая сцена.\n\n"
        "Отношения:\n"
        "Эдриан - доверие 10\n"
    )
    return {
        "user_input": user_input,
        "scene_output": scene,
        "extracted": {
            "persistence_reviewed": True,
            "chronology": [{"event": "Наступили следующие сутки."}],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
            "state_patch": {"current": {"present_characters": ["pov", "present"]}},
        },
    }


def prepare_commit(sid: str, payload: dict) -> dict:
    manifest = session_runtime.prepare_turn_packet(sid, payload["user_input"])
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    prepared = dict(payload)
    prepared["packet_id"] = manifest["packet_id"]
    return prepared


def commit_one(sid: str, user_input: str) -> tuple[dict, dict]:
    payload = prepare_commit(sid, valid_payload(user_input))
    return payload, commit_turn_request(sid, payload)


def test_exact_duplicate_commit_replays_atomic_receipt_without_second_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = prepare_commit(sid, valid_payload())

        first = commit_turn_request(sid, payload)
        assert first["turn_number"] == 1
        assert first["already_committed"] is False
        assert len(storage._read_turns(root)) == 1
        assert (root / RECEIPTS_FILE).exists()

        second = commit_turn_request(sid, payload)
        assert second["ok"] is True
        assert second["turn_number"] == 1
        assert second["already_completed"] is True
        assert second["idempotent_replay"] is True
        assert len(storage._read_turns(root)) == 1
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1


def test_same_packet_id_with_changed_payload_is_rejected_without_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = prepare_commit(sid, valid_payload())
        commit_turn_request(sid, payload)

        changed = dict(payload)
        changed["scene_output"] = payload["scene_output"].replace("Тестовая сцена.", "Другая сцена.")
        with pytest.raises(OperationReceiptConflict):
            commit_turn_request(sid, changed)
        assert len(storage._read_turns(root)) == 1


def test_duplicate_turn_replays_even_after_audit_gate_activates():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        last_payload = None
        for number in range(1, 16):
            last_payload, result = commit_one(sid, f"Ход {number}")
            assert result["turn_number"] == number

        assert last_payload is not None
        assert storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})["audit_required"] is True
        replay = commit_turn_request(sid, last_payload)
        assert replay["turn_number"] == 15
        assert replay["idempotent_replay"] is True
        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 15


def test_audit_exact_retry_returns_prior_success_without_second_audit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        for number in range(1, 16):
            commit_one(sid, f"Ход {number}")

        manifest = audit_runtime.get_audit_snapshot(sid)
        for index in range(manifest["chunk_count"]):
            audit_runtime.get_audit_snapshot_chunk(sid, manifest["audit_id"], index)
        payload = {
            "audit_id": manifest["audit_id"],
            "start_turn": 1,
            "end_turn": 15,
            "repairs": {},
            "notes": [],
        }
        first = commit_audit_request(sid, payload)
        second = commit_audit_request(sid, payload)
        assert first["audited_through"] == 15
        assert second["audited_through"] == 15
        assert second["idempotent_replay"] is True
        assert len(storage._read_json(root / "audits.json", [])) == 1


def test_rollback_retry_cannot_remove_a_replacement_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        commit_one(sid, "Первый ход")
        resume = session_runtime.continue_session(sid)
        request = {
            "expected_turn_number": 1,
            "expected_turn_id": resume["current_turn_id"],
            "confirm": True,
        }

        first = rollback_last_turn_request(sid, request)
        second = rollback_last_turn_request(sid, request)
        assert first["turn_number"] == 0
        assert second["turn_number"] == 0
        assert second["idempotent_replay"] is True

        replacement_payload, replacement = commit_one(sid, "Новый первый ход")
        assert replacement["turn_number"] == 1
        stale = rollback_last_turn_request(sid, request)
        assert stale["idempotent_replay"] is True
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1
        assert storage._read_turns(root)[-1]["user_input"] == replacement_payload["user_input"]

        fresh = session_runtime.continue_session(sid)
        fresh_request = {
            "expected_turn_number": 1,
            "expected_turn_id": fresh["current_turn_id"],
            "confirm": True,
        }
        rolled = rollback_last_turn_request(sid, fresh_request)
        assert rolled["turn_number"] == 0
        assert storage._read_turns(root) == []
