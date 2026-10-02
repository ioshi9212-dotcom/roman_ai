import tempfile
from pathlib import Path

import pytest

from app import storage
from app.operation_service import prepare_turn_request, commit_turn_request


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "relationship-review-gate",
        "title": "Relationship Review Gate",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "npc", "name": "NPC"},
            {"character_id": "ghost", "name": "Ghost"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov", "npc"]},
            "relationships": {"npc": {"доверие": 10}, "ghost": {"интерес": 20}},
        },
    }


def _read_all(sid: str, manifest: dict) -> None:
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)


def _reviewed_payload(
    manifest: dict,
    *,
    relationship_reviewed: bool,
    relationship_review=None,
    relationship_updates=None,
    scene_output: str = "Сцена продолжается.",
) -> dict:
    extracted = {
        "scene_builder_reviewed": True,
        "persistence_reviewed": True,
        "knowledge_reviewed": True,
        "relationship_reviewed": relationship_reviewed,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
        "relationship_updates": relationship_updates or [],
    }
    if relationship_review is not None:
        extracted["relationship_review"] = relationship_review
    return {
        "packet_id": manifest["packet_id"],
        "user_input": "(молча посмотреть)",
        "scene_output": scene_output,
        "extracted": extracted,
    }


def test_new_packet_requires_relationship_review():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review-required")
        assert manifest["relationship_review_required"] is True
        assert manifest["relationship_review_details_required"] is True
        assert manifest["relationship_footer_scope_required"] is True
        _read_all(sid, manifest)

        with pytest.raises(RuntimeError, match="RELATIONSHIP_REVIEW_REQUIRED"):
            commit_turn_request(sid, _reviewed_payload(manifest, relationship_reviewed=False))


def test_review_flag_alone_is_not_enough_anymore():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review-detail")
        _read_all(sid, manifest)

        with pytest.raises(RuntimeError, match="RELATIONSHIP_REVIEW_DETAIL_REQUIRED"):
            commit_turn_request(sid, _reviewed_payload(manifest, relationship_reviewed=True))


def test_changed_review_requires_matching_relationship_update():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review-changed")
        _read_all(sid, manifest)

        with pytest.raises(RuntimeError, match="RELATIONSHIP_REVIEW_CHANGED_WITHOUT_UPDATE"):
            commit_turn_request(
                sid,
                _reviewed_payload(
                    manifest,
                    relationship_reviewed=True,
                    relationship_review=[{"character_id": "npc", "changed": True, "reason": "Сцена изменила отношение."}],
                ),
            )


def test_unchanged_review_can_commit_without_update():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review-stable")
        _read_all(sid, manifest)

        result = commit_turn_request(
            sid,
            _reviewed_payload(
                manifest,
                relationship_reviewed=True,
                relationship_review=[{"character_id": "npc", "changed": False}],
            ),
        )
        assert result["already_committed"] is False


def test_remote_participant_must_be_reviewed_but_not_printed_in_footer():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["current"]["remote_characters"] = ["ghost"]
        storage._write_json(root / "state.json", state)

        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review-remote")
        _read_all(sid, manifest)
        with pytest.raises(RuntimeError, match="RELATIONSHIP_REVIEW_DETAIL_REQUIRED"):
            commit_turn_request(
                sid,
                _reviewed_payload(
                    manifest,
                    relationship_reviewed=True,
                    relationship_review=[{"character_id": "npc", "changed": False}],
                ),
            )


def test_visible_footer_rejects_absent_registered_npc():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="footer-scope")
        _read_all(sid, manifest)
        scene_output = "Сцена.\n\nОтношения:\nGhost - интерес 20\n\nХод 1"

        with pytest.raises(RuntimeError, match="RELATIONSHIP_FOOTER_ABSENT_NPC"):
            commit_turn_request(
                sid,
                _reviewed_payload(
                    manifest,
                    relationship_reviewed=True,
                    relationship_review=[{"character_id": "npc", "changed": False}],
                    scene_output=scene_output,
                ),
            )


def test_old_pending_packet_without_new_markers_remains_compatible():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(молча посмотреть)", request_id="legacy-pending")
        _read_all(sid, manifest)

        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        packet.pop("relationship_review_required", None)
        packet.pop("relationship_review_details_required", None)
        packet.pop("relationship_footer_scope_required", None)
        storage._write_json(root / "turn_packet.json", packet)

        result = commit_turn_request(sid, _reviewed_payload(manifest, relationship_reviewed=False))
        assert result["already_committed"] is False
