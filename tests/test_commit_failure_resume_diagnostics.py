"""A rejected GPT Action commit must be inspectable without changing canon."""
import pytest
from fastapi import HTTPException

from app import commit_failure_diagnostics as diagnostics, main, storage
from app.models import AuditCommit, CommitTurnRequest


@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(storage, "SESSIONS_DIR", root)
    sid = "example-session"
    target = root / sid
    target.mkdir()
    storage._write_json(target / "meta.json", {"turn_number": 30, "audit_required": False})
    (target / "turns.jsonl").write_text('{"turn_number":30}\n', encoding="utf-8")
    return sid, target


def _turn(packet_id="pkt31"):
    return CommitTurnRequest(
        packet_id=packet_id,
        user_input="(ждать)",
        scene_output="Игровая сцена.",
        extracted={},
    )


def test_direct_http_409_is_saved_for_resume_without_changing_canon(session, monkeypatch):
    sid, root = session
    def rejected(*args, **kwargs):
        raise HTTPException(status_code=409, detail={
            "code": "STORY_PROGRESS_REQUIRED",
            "message": "Scene has not progressed.",
            "stagnant_turns_before_this_commit": 3,
        })

    monkeypatch.setattr(main, "commit_turn_request", rejected)
    monkeypatch.setattr(main, "continue_session", lambda session_id: {"turn_number": 30})
    old_meta = (root / "meta.json").read_bytes()
    old_turns = (root / "turns.jsonl").read_bytes()

    with pytest.raises(HTTPException) as error:
        main.turns_commit(sid, _turn())
    assert error.value.status_code == 409

    resumed = main.session_resume(sid)
    failure = resumed["last_commit_rejection"]
    assert failure["code"] == "STORY_PROGRESS_REQUIRED"
    assert failure["committed"] is False
    assert failure["identity"] == "pkt31"
    assert failure["detail"]["stagnant_turns_before_this_commit"] == 3
    assert (root / "meta.json").read_bytes() == old_meta
    assert (root / "turns.jsonl").read_bytes() == old_turns


def test_success_clears_previous_rejection(session, monkeypatch):
    sid, _ = session
    diagnostics.record(sid, operation="commitTurn", identity="pkt31",
                       status_code=409, detail="TURN_PACKET_INCOMPLETE")
    monkeypatch.setattr(main, "commit_turn_request", lambda *args, **kwargs: {"ok": True, "turn_number": 31})
    assert main.turns_commit(sid, _turn())["turn_number"] == 31
    assert diagnostics.latest(sid) is None


@pytest.mark.parametrize("via_turns", [False, True])
def test_audit_http_409_is_recoverable_from_resume(session, monkeypatch, via_turns):
    sid, root = session
    storage._write_json(root / "meta.json", {"turn_number": 30, "audit_required": True})
    def rejected(*args, **kwargs):
        raise HTTPException(status_code=409, detail={
            "code": "SCENE_COMPACTION_COVERAGE_INVALID",
            "message": "Scene ranges have gaps.",
        })
    monkeypatch.setattr(main, "commit_audit_request", rejected)
    with pytest.raises(HTTPException):
        if via_turns:
            main.turns_commit(sid, CommitTurnRequest(audit_id="audit30", start_turn=16, end_turn=30))
        else:
            main.audit_commit(sid, AuditCommit(audit_id="audit30", start_turn=16, end_turn=30))

    result = diagnostics.latest(sid)
    assert result["operation"] == "commitAudit"
    assert result["identity"] == "audit30"
    assert result["code"] == "SCENE_COMPACTION_COVERAGE_INVALID"


def test_stale_failure_not_exposed_after_turn_advances(session):
    sid, root = session
    diagnostics.record(sid, operation="commitTurn", identity="pkt31",
                       status_code=409, detail="SCENE_BUILDER_REVIEW_REQUIRED")
    assert diagnostics.latest(sid)
    storage._write_json(root / "meta.json", {"turn_number": 31, "audit_required": False})
    assert diagnostics.latest(sid) is None


def test_unsafe_details_and_long_text_are_not_persisted(session):
    sid, root = session
    diagnostics.record(sid, operation="commitTurn", identity="pkt31",
                       status_code=409, detail={
                           "code": "RELATIONSHIP_REVIEW_DETAIL_REQUIRED",
                           "message": "Fix review",
                           "sensitive_prompt": "secret passage omitted from error report",
                           "missing_character_ids": ["npc_1"],
                       })
    report = diagnostics.latest(sid)
    assert report["code"] == "RELATIONSHIP_REVIEW_DETAIL_REQUIRED"
    assert report["detail"]["missing_character_ids"] == ["npc_1"]
    assert "sensitive_prompt" not in (root / "last_commit_rejection.json").read_text(encoding="utf-8")
