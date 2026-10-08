"""Recover a 111-turn session without replaying memory or old pending packets."""
import json
from copy import deepcopy

import pytest
from fastapi import HTTPException

from app import commit_failure_diagnostics, main, runtime_access, session_checkpoint, storage
from app.models import CommitTurnRequest


@pytest.fixture
def long_session(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(storage, "SESSIONS_DIR", root)
    sid = "long111"
    folder = root / sid
    folder.mkdir()
    storage._write_json(folder / "meta.json", {
        "turn_number": 111, "last_audit_turn": 105, "data_schema_version": 2,
        "audit_required": False,
    })
    storage._write_json(folder / "state.json", {
        "pov": {"character_id": "rina"},
        "current": {
            "date": "04.04.2026", "time": "20:50", "location": "Дом Эдриана",
            "scene": "Рината ждёт", "present_characters": ["rina"],
        },
    })
    (folder / "turns.jsonl").write_text(
        "".join(json.dumps({
            "turn_number": n, "scene_output": f"Сцена {n}", "extracted": {},
        }, ensure_ascii=False) + "\n" for n in range(1, 112)),
        encoding="utf-8",
    )
    return sid, folder


def test_old_packet_after_rollback_is_reported_stale_without_data_mutation(long_session, monkeypatch):
    sid, folder = long_session
    storage._write_json(folder / "turn_packet.json", {
        "packet_id": "ne84tmS_HKBy7v7i", "prepared_for_turn": 112,
        "request_id": "req112",
        "user_input": "(Зайти в дом и подготовить средства для обработки кисти)",
        "chunks": ["part1", "part2"], "read_chunks": [0, 1],
        "turn_pipeline_version": 21, "runtime_revision": "previous-runtime",
        "data_schema_version": 2,
    })
    monkeypatch.setattr(main, "continue_session", lambda *_: (_ for _ in ()).throw(AssertionError("heavy resume called")))
    original_files = {
        file: (folder / file).read_bytes()
        for file in ("meta.json", "state.json", "turns.jsonl", "turn_packet.json")
    }
    status = main.session_resume(sid)
    assert status["ok"] is True
    assert status["resume_checkpoint_version"] == 1
    assert status["turn_number"] == 111
    assert status["last_committed_turn"]["turn_number"] == 111
    assert status["last_committed_turn"]["scene_output"] == "Сцена 111"
    assert status["pending_turn"]["runtime_stale"] is True
    assert status["pending_turn"]["ready_for_commit"] is False
    assert status["pending_turn"]["packet_id"] == "ne84tmS_HKBy7v7i"
    assert "Do NOT commit" in status["instruction"]
    assert all((folder / name).read_bytes() == data for name, data in original_files.items())


def test_current_packet_can_be_reused_without_full_resume(long_session, monkeypatch):
    sid, folder = long_session
    storage._write_json(folder / "turn_packet.json", {
        "packet_id": "good112", "prepared_for_turn": 112,
        "user_input": "(ждать)", "chunks": ["a"], "read_chunks": [0],
        "turn_pipeline_version": 19,
        "runtime_revision": runtime_access.runtime_revision(),
        "data_schema_version": 2,
    })
    monkeypatch.setattr(main, "continue_session", lambda *_: (_ for _ in ()).throw(AssertionError("heavy resume called")))
    status = main.session_resume(sid)
    assert status["pending_turn"]["runtime_stale"] is False
    assert status["pending_turn"]["ready_for_commit"] is True
    assert "Use this exact pending packet" in status["instruction"]


def test_rejection_code_returned_with_resume_even_if_commit_action_hides_it(long_session, monkeypatch):
    sid, folder = long_session

    def rejected(*_args, **_kwargs):
        raise HTTPException(409, detail={"code": "CURRENT_STATE_PATCH_INVALID", "message": "Technical conflict"})

    monkeypatch.setattr(main, "commit_turn_request", rejected)
    body = CommitTurnRequest(
        packet_id="invalid112", user_input="(ждать)", scene_output="Текст сцены",
        extracted={"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True},
    )
    with pytest.raises(HTTPException) as error:
        main.turns_commit(sid, body)
    assert error.value.status_code == 409
    status = main.session_resume(sid)
    assert status["last_commit_rejection"]["code"] == "CURRENT_STATE_PATCH_INVALID"
    assert status["last_commit_rejection"]["status_code"] == 409
    assert status["last_committed_turn"]["turn_number"] == 111


def test_an_unexpected_server_exception_keeps_canon_untouched(long_session, monkeypatch):
    sid, folder = long_session
    before = (folder / "turns.jsonl").read_bytes()
    monkeypatch.setattr(main, "commit_turn_request", lambda *_: (_ for _ in ()).throw(KeyError("private scene contents")))
    body = CommitTurnRequest(
        packet_id="bad112", user_input="(ждать)", scene_output="Текст сцены", extracted={},
    )
    with pytest.raises(KeyError):
        main.turns_commit(sid, body)
    status = main.session_resume(sid)
    assert status["last_commit_rejection"]["code"] == "COMMIT_INTERNAL_ERROR"
    assert status["last_commit_rejection"]["status_code"] == 500
    assert (folder / "turns.jsonl").read_bytes() == before


def test_checkpoint_returns_an_archival_mismatch_without_fixing_files(long_session):
    sid, folder = long_session
    meta = storage._read_json(folder / "meta.json", {})
    meta["turn_number"] = 112
    storage._write_json(folder / "meta.json", meta)
    status = session_checkpoint.resume_checkpoint(sid)
    assert status["archived_turn_number"] == 111
    assert status["turn_archive_mismatch"] == {
        "meta_turn_number": 112, "last_archived_turn_number": 111,
    }
