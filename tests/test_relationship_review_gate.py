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
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov", "npc"]},
            "relationships": {"npc": {"доверие": 10}},
        },
    }


def test_relationship_review_capability_is_stored_and_can_upgrade_pending_turn():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        first = prepare_turn_request(sid, "(молча посмотреть)", request_id="rel-review")
        assert first["relationship_review_capable"] is False

        upgraded = prepare_turn_request(
            sid,
            "(молча посмотреть)",
            request_id="rel-review",
            relationship_review_capable=True,
        )
        assert upgraded["packet_id"] == first["packet_id"]
        assert upgraded["relationship_review_capable"] is True
        packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert packet["relationship_review_capable"] is True


def test_missing_relationship_review_flag_does_not_brick_live_pending_turn():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_turn_request(
            sid,
            "(молча посмотреть)",
            request_id="rel-review-required",
            relationship_review_capable=True,
        )
        start = 1 if manifest.get("first_chunk_included") else 0
        for index in range(start, manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(молча посмотреть)",
                "scene_output": "not reached",
                "extracted": {
                    "persistence_reviewed": True,
                    "relationship_reviewed": False,
                },
            },
        )
        assert result["already_committed"] is False
