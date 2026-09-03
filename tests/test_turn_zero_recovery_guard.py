import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from app import session_recovery, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "turn_zero_recovery",
        "title": "Turn Zero Recovery",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "дом Эдриана",
                "scene": "гостиная",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def test_turn_zero_recovery_normalises_saved_flat_start_without_rewriting_source():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        malformed_source = storage._read_json(root / "source.json", {})
        malformed_source["starting_state"] = {
            "pov": "Рината",
            "location": "дом Эдриана",
            "scene": "гостиная",
            "present_character_ids": ["Рината", "Эдриан"],
        }
        storage._write_json(root / "source.json", malformed_source)
        source_before = deepcopy(storage._read_json(root / "source.json", {}))

        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        storage._write_json(root / "state.json", state)

        assert session_recovery.current_recovery_status(sid)["required"] is True
        repaired = session_recovery.recover_session_current(sid)

        assert repaired["changed"] is True
        assert repaired["turn_number"] == 0
        assert repaired["turn_created"] is False
        assert repaired["canon_mutated"] is False
        assert repaired["current"]["location"] == "дом Эдриана"
        assert repaired["current"]["scene"] == "гостиная"
        assert repaired["current"]["present_characters"] == ["rina", "adrian"]
        assert session_recovery.current_recovery_status(sid)["required"] is False
        assert storage._read_json(root / "source.json", {}) == source_before
        assert storage._read_turns(root) == []


def test_turn_zero_recovery_can_use_neutral_fallback_when_saved_start_has_no_pointer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        source = storage._read_json(root / "source.json", {})
        source["starting_state"] = {"pov": "Рината"}
        storage._write_json(root / "source.json", source)
        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        storage._write_json(root / "state.json", state)

        repaired = session_recovery.recover_session_current(sid)
        assert repaired["turn_number"] == 0
        assert repaired["current"]["location"] == "Стартовая локация"
        assert repaired["current"]["scene"] == "стартовая сцена"
        assert repaired["current"]["present_characters"] == ["rina"]
        assert session_recovery.current_recovery_status(sid)["required"] is False


def test_no_evidence_fallback_is_for_turn_zero_only():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        source = storage._read_json(root / "source.json", {})
        source["starting_state"] = {}
        storage._write_json(root / "source.json", source)

        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        state["characters"] = {}
        storage._write_json(root / "state.json", state)

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 1
        storage._write_json(root / "meta.json", meta)

        with pytest.raises(RuntimeError, match="CURRENT_RECOVERY_NO_EVIDENCE"):
            session_recovery.recover_session_current(sid)
