"""Regression coverage: maintenance must not interrupt interactive gameplay."""
import pytest

from app import audit_runtime, operation_service, storage
from app.main import session_resume
from test_generator_state_documents import create, prepare, story


def _commit(sid, manifest, raw):
    return operation_service.commit_turn_request(sid, {
        "packet_id": manifest["packet_id"],
        "user_input": raw,
        "scene_output": "**Рен** - Продолжаем.",
        "extracted": {
            "scene_builder_reviewed": True,
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
        },
    })


def test_deferred_audit_window_is_contiguous_even_when_many_turns_pass():
    meta = {"turn_number": 64, "last_audit_turn": 0, "audit_required": True}
    assert audit_runtime._audit_range(meta) == (1, 15)
    meta["last_audit_turn"] = 15
    assert audit_runtime._audit_range(meta) == (16, 30)
    meta["last_audit_turn"] = 60
    assert audit_runtime._audit_range(meta) == (61, 64)


def test_gameplay_resumes_past_fifteen_without_auto_audit(story):
    sid, root = create(story)
    for number in range(1, 17):
        raw = f"Продолжаю разговор {number}"
        manifest, context = prepare(sid, raw, request_id=f"audit-free-{number}")
        assert context["session"]["audit_blocking"] is False
        saved = _commit(sid, manifest, raw)
        assert saved["turn_number"] == number
        assert "required_audit" not in saved

    state = storage._read_json(root / "meta.json", {})
    assert state["turn_number"] == 16
    assert state["audit_required"] is True
    assert len(storage._read_turns(root)) == 16
    assert not (root / "audit_packet.json").exists()

    resumed = session_resume(sid)
    assert resumed["audit_available"] is True
    assert resumed["audit_blocking"] is False
    assert "audit_manifest" not in resumed
    assert not (root / "audit_packet.json").exists()

    manual = session_resume(sid, prepare_audit=True)
    packet = manual["audit_manifest"]
    assert packet["audit_range"] == [1, 15]
    assert packet["first_chunk_included"] is True

    # A new committed scene invalidates any prepared audit, not the raw turns.
    raw = "Продолжаю разговор 17"
    manifest, _ = prepare(sid, raw, request_id="audit-free-17")
    assert _commit(sid, manifest, raw)["turn_number"] == 17
    with pytest.raises(RuntimeError, match="AUDIT_PACKET_STALE"):
        audit_runtime.require_complete_audit_read(sid, 1, 15, audit_id=packet["audit_id"])

    refreshed = session_resume(sid, prepare_audit=True)["audit_manifest"]
    assert refreshed["audit_range"] == [1, 15]
    assert refreshed["audit_id"] != packet["audit_id"]
    assert len(storage._read_turns(root)) == 17
