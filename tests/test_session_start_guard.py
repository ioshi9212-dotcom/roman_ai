import json
import tempfile
from pathlib import Path

from app import storage
from app.novel_drafts import create_draft, create_session_from_draft, finalize_draft, save_section
from app.session_recovery import current_recovery_status


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def base_novel(starting_state=None):
    value = {
        "novel_id": "start_guard",
        "title": "Start Guard",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "lore": {},
    }
    if starting_state is not None:
        value["starting_state"] = starting_state
    return value


def test_flat_starting_state_is_lifted_into_current_before_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel(
            {
                "pov": "Рината",
                "date": "03.09.2026",
                "time": "12:00",
                "location": "дом Эдриана",
                "scene": "утро в гостиной",
                "present_characters": ["Рината", "Эдриан"],
            }
        )

        sid = storage.create_session(novel)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        source = storage._read_json(storage.SESSIONS_DIR / sid / "source.json", {})

        assert state["current"]["date"] == "03.09.2026"
        assert state["current"]["time"] == "12:00"
        assert state["current"]["location"] == "дом Эдриана"
        assert state["current"]["scene"] == "утро в гостиной"
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert state["pov"]["character_id"] == "rina"
        assert source["starting_state"]["current"]["location"] == "дом Эдриана"
        assert current_recovery_status(sid)["required"] is False


def test_scene_state_alias_and_present_character_ids_are_normalised():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel(
            {
                "pov_character_id": "Рината",
                "scene_state": {
                    "location_name": "кухня",
                    "game_time": "08:10",
                    "situation": "завтрак",
                    "present_character_ids": ["Рината", "Эдриан"],
                },
            }
        )

        sid = storage.create_session(novel)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state["current"]["location"] == "кухня"
        assert state["current"]["time"] == "08:10"
        assert state["current"]["scene"] == "завтрак"
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert state["pov"]["character_id"] == "rina"
        assert current_recovery_status(sid)["required"] is False


def test_missing_roster_reinserts_pov_instead_of_creating_broken_session():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel(
            {
                "pov": {"character_id": "rina"},
                "current": {"location": "спальня"},
            }
        )

        sid = storage.create_session(novel)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state["current"]["present_characters"] == ["rina"]
        assert current_recovery_status(sid)["required"] is False


def test_missing_starting_state_gets_neutral_turn_zero_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state["current"]["location"] == "Стартовая локация"
        assert state["current"]["scene"] == "стартовая сцена"
        assert state["current"]["present_characters"] == ["rina"]
        assert state["pov"]["character_id"] == "rina"
        assert current_recovery_status(sid)["required"] is False


def test_draft_flow_cannot_create_unrecoverable_turn_zero_session():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft = create_draft("draft_guard", "Draft Guard", 1)
        draft_id = draft["draft_id"]

        save_section(
            draft_id,
            "novel",
            json.dumps({"pov_character": "Рината"}, ensure_ascii=False),
        )
        save_section(
            draft_id,
            "characters",
            json.dumps(
                [
                    {"character_id": "rina", "name": "Рината", "is_pov": True},
                    {"character_id": "adrian", "name": "Эдриан"},
                ],
                ensure_ascii=False,
            ),
        )
        save_section(draft_id, "lore", "{}")
        save_section(
            draft_id,
            "starting_state",
            json.dumps(
                {
                    "pov": "Рината",
                    "location": "дом Эдриана",
                    "present_character_ids": ["Рината", "Эдриан"],
                },
                ensure_ascii=False,
            ),
        )
        finalize_draft(draft_id)

        sid = create_session_from_draft(draft_id)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state["current"]["location"] == "дом Эдриана"
        assert state["current"]["present_characters"] == ["rina", "adrian"]
        assert current_recovery_status(sid)["required"] is False
