"""Long-session resume must not rebuild all persistent memory or reread turn JSON."""
import json

import pytest

from app import commit_failure_diagnostics, fast_resume_checkpoint, main, storage
from app.models import CommitTurnRequest


@pytest.fixture
def long_session(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    sid = "long-session"
    data = root / sid
    data.mkdir()
    monkeypatch.setattr(storage, "SESSIONS_DIR", root)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)

    storage._write_json(data / "meta.json", {
        "turn_number": 111, "audit_required": False, "last_audit_turn": 105,
        "data_schema_version": 2,
    })
    storage._write_json(data / "source.json", {
        "novel_id": "test", "title": "Novel",
        "novel": {"pov_character": "pov"},
        "characters": [{"character_id": "pov", "name": "Rina", "is_pov": True}],
    })
    storage._write_json(data / "state.json", {
        "pov": {"character_id": "pov"},
        "current": {
            "date": "04.04.2026", "time": "20:50", "location": "Home",
            "scene": "Waiting", "present_characters": ["pov"],
        },
    })
    (data / "turns.jsonl").write_text(
        "".join(json.dumps({"turn_number": number, "scene_output": f"Scene {number}", "extracted": {}}, ensure_ascii=False) + "\n" for number in range(1, 112)),
        encoding="utf-8",
    )
    return sid, data


def test_resume_long_session_is_read_only_and_does_not_call_heavy_continue(long_session, monkeypatch):
    sid, root = long_session
    from app import runtime_access
    storage._write_json(root / "turn_packet.json", {
        "packet_id": "packet112", "prepared_for_turn": 112,
        "request_id": "request112", "user_input": "(ждать)", "chunks": ["{}"],
        "chunk_count": 1, "read_chunks": [0],
        "data_schema_version": 2, "runtime_revision": runtime_access.runtime_revision(),
        "turn_pipeline_version": 21,
    })
    monkeypatch.setattr(main, "continue_session", lambda sid: (_ for _ in ()).throw(AssertionError("heavy resume called")))
    original_meta = (root / "meta.json").read_bytes()
    original_turns = (root / "turns.jsonl").read_bytes()
    status = main.session_resume(sid)
    assert status["turn_number"] == 111
    assert status["last_committed_turn"]["turn_number"] == 111
    assert status["last_committed_turn"]["scene_output"] == "Scene 111"
    assert status["pending_turn"]["packet_id"] == "packet112"
    assert status["pending_turn"]["ready_for_commit"] is True
    assert status["pending_turn"]["runtime_stale"] is False
    assert status["resume_mode"] == "bounded_read_only"
    assert status["current_recovery_required"] is False
    assert status["current_turn_id"]
    assert (root / "meta.json").read_bytes() == original_meta
    assert (root / "turns.jsonl").read_bytes() == original_turns


def test_resume_reports_stale_packet_without_erasing_it(long_session):
    sid, root = long_session
    storage._write_json(root / "turn_packet.json", {
        "packet_id": "oldpacket", "prepared_for_turn": 112,
        "user_input": "original", "chunks": ["{}"], "read_chunks": [0],
        "turn_pipeline_version": 0,
    })
    status = main.session_resume(sid)
    assert status["pending_turn"]["runtime_stale"] is True
    assert "prepareTurn again" in status["instruction"]
    assert storage._read_json(root / "turn_packet.json", {})["packet_id"] == "oldpacket"


def test_internal_commit_error_is_recorded_without_swallowing_exception(long_session, monkeypatch):
    sid, _ = long_session
    def crash(*args, **kwargs):
        raise KeyError("internal crash")
    monkeypatch.setattr(main, "commit_turn_request", crash)
    body = CommitTurnRequest(packet_id="packet112", user_input="test", scene_output="Scene", extracted={})
    with pytest.raises(KeyError):
        main.turns_commit(sid, body)
    record = commit_failure_diagnostics.latest(sid)
    assert record["code"] == "COMMIT_INTERNAL_ERROR"
    assert record["status_code"] == 500
    assert record["turn_number_at_failure"] == 111
