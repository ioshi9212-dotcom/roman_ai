import json
import tempfile
from pathlib import Path

import pytest

from app import relationship_file_runtime, session_migrations, session_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request
from app.runtime_access import runtime_revision


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "schema-migration",
        "title": "Schema Migration",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {
                "character_id": "npc",
                "name": "NPC",
                "relationships": [{
                    "target_character_id": "pov",
                    "dimensions": [{"label": "доверие", "value": 5}],
                }],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.10.2026",
                "time": "12:00",
                "location": "room",
                "present_characters": ["pov", "npc"],
            },
        },
    }


def test_new_session_is_stamped_with_current_data_schema():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        meta = storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})
        assert meta["data_schema_version"] == session_migrations.CURRENT_DATA_SCHEMA_VERSION


def test_old_session_migrates_in_place_without_rewriting_turn_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta.pop("data_schema_version", None)
        storage._write_json(root / "meta.json", meta)

        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {"npc": {"legacy": -4}}
        state["relationship_documents"] = {"npc": {"legacy": True}}
        storage._write_json(root / "state.json", state)

        relationships = storage._read_json(root / "relationships.json", {})
        relationships["npc_to_pov"]["npc"]["dimensions"] = {
            "доверие": {"value": -2, "last_change": {"turn": 0, "delta": 0, "reason": "old"}},
            "интерес": {"value": 140, "last_change": {"turn": 0, "delta": 0, "reason": "old"}},
        }
        storage._write_json(root / "relationships.json", relationships)

        original_turn = {
            "turn_number": 1,
            "user_input": "старый ход",
            "scene_output": "точный старый текст",
            "extracted": {},
        }
        (root / "turns.jsonl").write_text(
            json.dumps(original_turn, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 1
        storage._write_json(root / "meta.json", meta)

        resumed = session_runtime.continue_session(sid)

        meta = storage._read_json(root / "meta.json", {})
        assert meta["data_schema_version"] == session_migrations.CURRENT_DATA_SCHEMA_VERSION
        assert resumed["data_schema_version"] == session_migrations.CURRENT_DATA_SCHEMA_VERSION

        state = storage._read_json(root / "state.json", {})
        for key in session_migrations.LEGACY_RELATIONSHIP_KEYS:
            assert key not in state

        saved = storage._read_json(root / "relationships.json", {})
        dims = saved["npc_to_pov"]["npc"]["dimensions"]
        assert "доверие" not in dims
        assert dims["интерес"]["value"] == 100
        assert storage._read_turns(root) == [original_turn]


def test_runtime_change_rebuilds_uncommitted_packet_for_same_input():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        first = prepare_turn_request(sid, "тот же ход", request_id="request-1")
        packet = storage._read_json(root / "turn_packet.json", {})
        packet["runtime_revision"] = "obsolete"
        storage._write_json(root / "turn_packet.json", packet)

        second = prepare_turn_request(sid, "тот же ход", request_id="request-1")

        assert second["packet_id"] != first["packet_id"]
        current = storage._read_json(root / "turn_packet.json", {})
        assert current["runtime_revision"] == runtime_revision()
        assert current["data_schema_version"] == session_migrations.CURRENT_DATA_SCHEMA_VERSION
        abandoned = storage._read_json(root / "abandoned_turn_packets.json", [])
        assert abandoned[-1]["packet_id"] == first["packet_id"]
        assert abandoned[-1]["reason"] == "runtime_or_schema_changed"


def test_stale_packet_cannot_commit_after_runtime_change():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest = prepare_turn_request(sid, "ход", request_id="request-stale")
        for index in range(1, manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)

        packet = storage._read_json(root / "turn_packet.json", {})
        packet["runtime_revision"] = "obsolete"
        storage._write_json(root / "turn_packet.json", packet)

        with pytest.raises(RuntimeError, match="TURN_PACKET_RUNTIME_STALE"):
            commit_turn_request(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": "ход",
                    "scene_output": "не должен сохраниться",
                    "extracted": {},
                },
            )
        assert not (root / "turn_packet.json").exists()
        assert storage._read_turns(root) == []


def test_historical_relationship_replay_translates_old_payload_without_mutating_turns():
    source = novel()
    cards = storage._normalise_cards(source["characters"])
    turns = [
        {
            "turn_number": 1,
            "user_input": "old",
            "extracted": {
                "relationship_updates": [{
                    "character_id": "npc",
                    "reason": "old negative axis",
                    "dimensions": [{"label": "обида", "value": -2}],
                }],
            },
        },
        {
            "turn_number": 2,
            "user_input": "old absolute",
            "extracted": {
                "relationship_updates": [{
                    "character_id": "npc",
                    "dimensions": [{"label": "доверие", "value": 3}],
                }],
            },
        },
    ]
    original = json.loads(json.dumps(turns, ensure_ascii=False))

    rebuilt = relationship_file_runtime.rebuild_from_turns(source, cards, turns)

    dims = rebuilt["npc_to_pov"]["npc"]["dimensions"]
    assert dims["доверие"]["value"] == 3
    assert "обида" not in dims
    assert turns == original
