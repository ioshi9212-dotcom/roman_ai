import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from app import audit_runtime, session_runtime, storage
from app.rollback_snapshot_runtime import SNAPSHOT_FILE
from app.turn_rollback import RollbackError, rollback_last_turn


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def make_session() -> str:
    novel = {
        "novel_id": "rollback_test",
        "title": "Rollback Test",
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


def commit_simple_turn(sid: str, turn: int):
    user_input = f"Ход {turn}."
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    return session_runtime.commit_turn(
        sid,
        {
            "user_input": user_input,
            "scene_output": (
                "🎭 Rollback · осень\n"
                f"🕒 День 1 · вторник, 01.09.2026, 10:{turn % 60:02d} · 📍 room\n\n"
                f"Сцена {turn}.\n\nСостояние: нормально\nОтношения:\n\nХод {turn}"
            ),
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
            },
        },
    )


def close_audit(sid: str, start_turn: int, end_turn: int):
    manifest = audit_runtime.get_audit_snapshot(sid)
    assert manifest["audit_range"] == [start_turn, end_turn]
    for index in range(manifest["chunk_count"]):
        audit_runtime.get_audit_snapshot_chunk(sid, manifest["audit_id"], index)
    result = session_runtime.commit_audit(
        sid,
        {"start_turn": start_turn, "end_turn": end_turn, "repairs": {}, "notes": []},
    )
    audit_runtime.clear_audit_packet(sid)
    return result


def test_snapshot_rollback_restores_exact_pre_turn_canon_and_reopens_same_number():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid

        commit_simple_turn(sid, 1)
        snapshot = storage._read_json(root / SNAPSHOT_FILE, {})
        assert snapshot["committed_turn"] == 1
        assert snapshot["previous_turn"] == 0

        result = rollback_last_turn(sid, 1, True)
        assert result["method"] == "exact_pre_turn_snapshot"
        assert result["turn_number"] == 0
        assert result["ready_to_retry_turn"] == 1
        assert storage._read_turns(root) == []
        assert storage._read_json(root / "characters.json", []) == snapshot["characters"]
        assert storage._read_json(root / "state.json", {}) == snapshot["state"]
        assert storage._read_json(root / "memory.json", {}) == snapshot["memory"]
        assert storage._read_json(root / "chronology.json", []) == snapshot["chronology"]
        assert not (root / "turn_packet.json").exists()
        assert not (root / SNAPSHOT_FILE).exists()

        retry = session_runtime.prepare_turn_packet(sid, "Ход 1 заново.")
        assert retry["prepared_for_turn"] == 1


def test_historical_replay_rolls_back_pre_snapshot_turn_across_completed_audit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid

        for turn in range(1, 18):
            commit_simple_turn(sid, turn)
            if turn == 15:
                assert close_audit(sid, 1, 15)["audited_through"] == 15

        assert storage._read_json(root / "meta.json", {})["turn_number"] == 17
        (root / SNAPSHOT_FILE).unlink(missing_ok=True)

        result = rollback_last_turn(sid, 17, True)
        assert result["method"] == "verified_historical_replay"
        assert result["turn_number"] == 16
        assert result["ready_to_retry_turn"] == 17
        assert len(storage._read_turns(root)) == 16
        meta = storage._read_json(root / "meta.json", {})
        assert meta["turn_number"] == 16
        assert meta["last_audit_turn"] == 15
        assert meta["audit_required"] is False
        audits = storage._read_json(root / "audits.json", [])
        assert [item["end_turn"] for item in audits] == [15]

        retry = session_runtime.prepare_turn_packet(sid, "Исправленный ход 17.")
        assert retry["prepared_for_turn"] == 17


def test_historical_replay_mismatch_refuses_without_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid
        commit_simple_turn(sid, 1)
        (root / SNAPSHOT_FILE).unlink(missing_ok=True)

        state = storage._read_json(root / "state.json", {})
        state.setdefault("world", {})["unexplained_external_mutation"] = True
        storage._write_json(root / "state.json", state)
        before = {
            name: (root / name).read_bytes()
            for name in ("turns.jsonl", "characters.json", "state.json", "memory.json", "chronology.json", "meta.json")
        }

        with pytest.raises(RollbackError, match="ROLLBACK_REPLAY_MISMATCH"):
            rollback_last_turn(sid, 1, True)

        after = {name: (root / name).read_bytes() for name in before}
        assert after == before


def test_expected_turn_mismatch_and_missing_confirmation_are_non_mutating():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid
        commit_simple_turn(sid, 1)
        before = (root / "turns.jsonl").read_bytes()

        with pytest.raises(RollbackError, match="ROLLBACK_CONFIRMATION_REQUIRED"):
            rollback_last_turn(sid, 1, False)
        with pytest.raises(RollbackError, match="ROLLBACK_EXPECTED_TURN_MISMATCH"):
            rollback_last_turn(sid, 2, True)

        assert (root / "turns.jsonl").read_bytes() == before
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1
