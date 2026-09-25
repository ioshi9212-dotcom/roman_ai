import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.operation_service import prepare_turn_request
from app.scene_knowledge_read import (
    get_character_knowledge_chunk,
    prepare_character_knowledge_read,
    require_complete_scene_knowledge_reads,
    scene_knowledge_read_status,
)


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel(*, version: int = 4):
    value = {
        "novel_id": f"knowledge-read-v{version}",
        "title": "Knowledge Read",
        "version": version,
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "aiden", "name": "Эйден"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "location": "база",
                "present_characters": ["pov", "aiden"],
            },
        },
    }
    if version >= 5:
        value["profile_schema"] = {"version": 1}
    return value


def _read_all(session_id: str, character_id: str):
    manifest = prepare_character_knowledge_read(session_id, character_id)
    pieces = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        pieces.append(
            get_character_knowledge_chunk(
                session_id,
                character_id,
                manifest["read_id"],
                index,
            )["content"]
        )
    return manifest, json.loads("".join(pieces))


def test_legacy_present_character_reads_all_430_facts_in_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel(version=4))["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "aiden")
        bucket["knowledge"] = [
            {
                "fact_id": f"aiden-{i}",
                "fact": f"Знание Эйдена номер {i}: " + ("деталь " * 8),
                "learned_turn": i,
            }
            for i in range(1, 431)
        ]
        storage._write_json(root / "memory.json", memory)

        turn = prepare_turn_request(
            sid,
            "(посмотреть на Эйдена)",
            "full-knowledge-430",
            knowledge_review_capable=True,
            complete_knowledge_read_capable=True,
        )
        assert set(turn["scene_knowledge_reads"]["required_character_ids"]) == {"pov", "aiden"}
        assert "aiden" in turn["scene_knowledge_reads"]["incomplete_character_ids"]

        manifest, payload = _read_all(sid, "aiden")
        assert manifest["chunk_count"] > 1
        assert payload["entry_count"] == 430
        assert len(payload["knowledge"]) == 430
        assert payload["knowledge"][0]["fact_id"] == "aiden-1"
        assert payload["knowledge"][-1]["fact_id"] == "aiden-430"

        _read_all(sid, "pov")
        status = scene_knowledge_read_status(sid)
        assert status["all_complete"] is True
        assert status["incomplete_character_ids"] == []
        require_complete_scene_knowledge_reads(sid)


def test_v5_present_character_reads_entire_journal_not_last_window():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel(version=5))["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "aiden")
        bucket["knowledge_journal"] = [
            {
                "entry_id": f"journal-{i}",
                "date": "25.09.2026",
                "period": "день",
                "text": f"Журнальное знание номер {i}.",
                "turn": i,
            }
            for i in range(1, 151)
        ]
        storage._write_json(root / "memory.json", memory)

        prepare_turn_request(
            sid,
            "(молчать)",
            "full-v5-journal",
            knowledge_review_capable=True,
            complete_knowledge_read_capable=True,
        )
        _, payload = _read_all(sid, "aiden")
        assert payload["source_kind"] == "knowledge_journal"
        assert payload["entry_count"] == 150
        assert payload["knowledge"][0]["text"] == "Журнальное знание номер 1."
        assert payload["knowledge"][-1]["text"] == "Журнальное знание номер 150."


def test_commit_gate_reports_missing_character_reads():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel(version=4))["session_id"]
        prepare_turn_request(
            sid,
            "(продолжить)",
            "knowledge-gate",
            knowledge_review_capable=True,
            complete_knowledge_read_capable=True,
        )
        with pytest.raises(RuntimeError, match="CHARACTER_KNOWLEDGE_READ_REQUIRED"):
            require_complete_scene_knowledge_reads(sid)

        _read_all(sid, "pov")
        with pytest.raises(RuntimeError, match="CHARACTER_KNOWLEDGE_READ_REQUIRED"):
            require_complete_scene_knowledge_reads(sid)

        _read_all(sid, "aiden")
        require_complete_scene_knowledge_reads(sid)


def test_identical_pending_turn_can_upgrade_to_complete_knowledge_reads():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel(version=4))["session_id"]

        first = prepare_turn_request(
            sid,
            "(ждать)",
            "upgrade-pending",
            knowledge_review_capable=True,
            complete_knowledge_read_capable=False,
        )
        assert first["complete_knowledge_read_capable"] is False

        upgraded = prepare_turn_request(
            sid,
            "(ждать)",
            "upgrade-pending",
            knowledge_review_capable=True,
            complete_knowledge_read_capable=True,
        )
        assert upgraded["complete_knowledge_read_capable"] is True
        assert "scene_knowledge_reads" in upgraded
        packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert packet["complete_knowledge_read_capable"] is True
