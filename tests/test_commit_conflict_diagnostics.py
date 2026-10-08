"""Commit failures remain recoverable and leave searchable diagnostic codes."""
import logging

import pytest
from fastapi import HTTPException

from app import main
from app.models import AuditCommit, CommitTurnRequest


@pytest.mark.parametrize("through_public_turn_endpoint", [False, True])
def test_cross_date_memory_audit_rejection_returns_409_and_logs_code(
    monkeypatch, caplog, through_public_turn_endpoint
):
    def fail_audit(*args, **kwargs):
        raise RuntimeError("MEMORY_COMPACTION_CROSS_DATE")

    monkeypatch.setattr(main, "commit_audit_request", fail_audit)
    payload = {"audit_id": "audit_30", "start_turn": 16, "end_turn": 30}
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with pytest.raises(HTTPException) as raised:
            if through_public_turn_endpoint:
                main.turns_commit("session-example", CommitTurnRequest(**payload))
            else:
                main.audit_commit("session-example", AuditCommit(**payload))

    assert raised.value.status_code == 409
    assert "different dates or periods" in raised.value.detail
    assert "raw facts remain preserved" in raised.value.detail
    assert any(
        "commitAudit rejected" in record.message
        and "MEMORY_COMPACTION_CROSS_DATE" in record.message
        for record in caplog.records
    )


def test_commit_turn_validation_409_logs_exact_reason(monkeypatch, caplog):
    def fail_turn(*args, **kwargs):
        raise RuntimeError("RELATIONSHIP_REVIEW_DETAIL_REQUIRED")

    monkeypatch.setattr(main, "commit_turn_request", fail_turn)
    body = CommitTurnRequest(
        packet_id="packet-example",
        user_input="продолжить",
        scene_output="Сцена для теста.",
        extracted={},
    )
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with pytest.raises(HTTPException) as raised:
            main.turns_commit("session-example", body)

    assert raised.value.status_code == 409
    assert "relationship_review" in raised.value.detail
    assert any(
        "commitTurn rejected" in record.message
        and "RELATIONSHIP_REVIEW_DETAIL_REQUIRED" in record.message
        for record in caplog.records
    )
