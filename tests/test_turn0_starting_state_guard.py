import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.novel_drafts import (
    _draft_path,
    create_draft,
    create_session_from_draft,
    draft_status,
    finalize_draft,
    save_section,
)
from app.session_recovery import current_recovery_status


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def save_base(draft_id: str):
    save_section(draft_id, "novel", json.dumps({"pov_character": "rina"}))
    save_section(
        draft_id,
        "characters",
        json.dumps(
            [
                {"character_id": "rina", "name": "Рина", "is_pov": True},
                {"character_id": "adrian", "name": "Эдриан"},
            ],
            ensure_ascii=False,
        ),
    )
    save_section(draft_id, "lore", json.dumps({"world": "canon"}))


def test_starting_state_is_required_before_finalize():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("missing_start", "Missing Start")["draft_id"]
        save_base(draft_id)
        status = draft_status(draft_id)
        assert "starting_state" in status["missing_required_sections"]
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] is None
        with pytest.raises(ValueError, match="DRAFT_INCOMPLETE"):
            finalize_draft(draft_id)


def test_status_exposes_starting_state_blocker_before_finalize():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("blocked_start", "Blocked Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps({"pov": "Рина", "location": "дом"}, ensure_ascii=False),
        )
        status = draft_status(draft_id)
        assert status["missing_required_sections"] == []
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] == "STARTING_STATE_PRESENT_CHARACTERS_REQUIRED"
        with pytest.raises(ValueError, match="STARTING_STATE_PRESENT_CHARACTERS_REQUIRED"):
            finalize_draft(draft_id)


def test_finalize_rejects_start_without_scene_pointer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("empty_start", "Empty Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps({"pov": "Рина", "present_characters": ["Рина"]}, ensure_ascii=False),
        )
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] == "STARTING_STATE_SCENE_POINTER_REQUIRED"
        with pytest.raises(ValueError, match="STARTING_STATE_SCENE_POINTER_REQUIRED"):
            finalize_draft(draft_id)


def test_flat_gpt_starting_state_is_normalized_into_usable_current():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("flat_start", "Flat Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps(
                {"pov": "Рина", "location": "кухня", "scene": "утро дома", "present_characters": ["Эдриан"]},
                ensure_ascii=False,
            ),
        )
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert status["finalize_blocker"] is None
        assert finalize_draft(draft_id)["ok"] is True
        sid = create_session_from_draft(draft_id)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["pov"]["character_id"] == "rina"
        assert state["current"]["location"] == "кухня"
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert current_recovery_status(sid)["required"] is False


def test_common_gpt_aliases_are_normalized_before_finalize():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("alias_start", "Alias Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps(
                {"protagonist": "Рина", "start": {"current_location": "дом", "current_time": "09:00", "participants": ["Рина", "Эдриан"]}},
                ensure_ascii=False,
            ),
        )
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert finalize_draft(draft_id)["ok"] is True
        sid = create_session_from_draft(draft_id)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["current"]["location"] == "дом"
        assert state["current"]["time"] == "09:00"
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert current_recovery_status(sid)["required"] is False


def test_present_character_ids_alias_is_normalized():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("ids_alias_start", "IDs Alias Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps(
                {"pov": "rina", "location": "дом", "present_character_ids": ["rina", "adrian"]},
                ensure_ascii=False,
            ),
        )
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert status["finalize_blocker"] is None
        assert finalize_draft(draft_id)["ok"] is True
        sid = create_session_from_draft(draft_id)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert current_recovery_status(sid)["required"] is False


def test_time_only_pointer_matches_recovery_contract():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("time_start", "Time Start")["draft_id"]
        save_base(draft_id)
        save_section(
            draft_id,
            "starting_state",
            json.dumps({"pov": "Рина", "time": "09:00", "present_characters": ["Рина"]}, ensure_ascii=False),
        )
        assert draft_status(draft_id)["ready_to_finalize"] is True
        assert finalize_draft(draft_id)["ok"] is True
        sid = create_session_from_draft(draft_id)["session_id"]
        assert current_recovery_status(sid)["required"] is False


def test_create_session_revalidates_old_finalized_broken_draft():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("legacy_broken", "Legacy Broken")["draft_id"]
        save_base(draft_id)
        save_section(draft_id, "starting_state", json.dumps({"current": {"location": "room", "present_characters": ["rina"]}}))
        finalize_draft(draft_id)
        path = _draft_path(draft_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["finalized_template"]["starting_state"] = {"current": {}}
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match="STARTING_STATE_SCENE_POINTER_REQUIRED"):
            create_session_from_draft(draft_id)
