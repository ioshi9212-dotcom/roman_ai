import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "presence",
        "title": "Presence",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "jayden", "name": "Джейден"},
            {"character_id": "liam", "name": "Лиам"},
            {"character_id": "chloe", "name": "Хлоя"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "hall",
                "present_characters": ["rina", "jayden", "liam"],
            },
            "relationships": {
                "jayden": {"симпатия": 8},
                "liam": {"настороженность": 7},
                "chloe": {"доверие": 5},
            },
        },
    }


def reviewed(**extra):
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }
    value.update(extra)
    return value


def read_packet(session_id: str, user_input: str = "test"):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    raw = "".join(
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"]
        for index in range(manifest["chunk_count"])
    )
    return json.loads(raw)


def scene(rows: list[str], turn: int = 1):
    return "\n".join(
        [
            "🎭 Presence · осень",
            "",
            "Сцена.",
            "",
            "Состояние: спокойно",
            "Отношения:",
            *rows,
            "",
            f"Ход {turn} · цикл {turn}/15",
        ]
    )


def current_state(sid: str):
    return storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})["current"]


def test_packet_exposes_old_generator_style_scene_focus_and_transition_contract():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        packet = read_packet(sid)
        assert packet["scene_focus"]["pov_character_id"] == "rina"
        assert packet["scene_focus"]["present_character_ids"] == ["rina", "jayden", "liam"]
        assert packet["scene_focus"]["required_full_character_ids"] == ["rina", "jayden", "liam"]
        assert packet["scene_presence"]["final_roster_formula"] == "start roster + enter - leave; move does not change membership"
        assert packet["scene_presence"]["direct_roster_omission_cannot_remove"] is True


def test_omitting_present_npc_from_direct_state_patch_cannot_make_them_disappear():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene([
                    "Джейден - симпатия 8",
                    "Лиам - настороженность 7",
                ]),
                "extracted": reviewed(
                    state_patch={"current": {"present_characters": ["rina", "liam"]}}
                ),
            },
        )
        current = current_state(sid)
        assert current["present_characters"] == ["rina", "jayden", "liam"]
        assert current["entered_characters"] == []
        assert current["left_characters"] == []


def test_move_keeps_character_present_and_updates_position():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene([
                    "Джейден - симпатия 8",
                    "Лиам - настороженность 7",
                ]),
                "extracted": reviewed(
                    presence_updates=[
                        {
                            "character_id": "jayden",
                            "action": "move",
                            "zone": "window",
                            "note": "a few steps from POV",
                        }
                    ]
                ),
            },
        )
        current = current_state(sid)
        assert "jayden" in current["present_characters"]
        assert current["left_characters"] == []
        assert current["positions"]["jayden"]["zone"] == "window"
        assert current["positions"]["jayden"]["note"] == "a few steps from POV"


def test_explicit_leave_is_the_only_way_to_remove_existing_character():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene(["Лиам - настороженность 7"]),
                "extracted": reviewed(
                    presence_updates=[{"character_id": "jayden", "action": "leave"}]
                ),
            },
        )
        current = current_state(sid)
        assert current["present_characters"] == ["rina", "liam"]
        assert current["left_characters"] == ["jayden"]


def test_enter_adds_character_to_final_roster():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene([
                    "Джейден - симпатия 8",
                    "Лиам - настороженность 7",
                    "Хлоя - доверие 5",
                ]),
                "extracted": reviewed(
                    presence_updates=[
                        {"character_id": "chloe", "action": "enter", "zone": "doorway"}
                    ]
                ),
            },
        )
        current = current_state(sid)
        assert current["present_characters"] == ["rina", "jayden", "liam", "chloe"]
        assert current["entered_characters"] == ["chloe"]
        assert current["positions"]["chloe"]["zone"] == "doorway"


def test_pov_cannot_be_removed_from_scene_roster():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid)
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene([
                        "Джейден - симпатия 8",
                        "Лиам - настороженность 7",
                    ]),
                    "extracted": reviewed(
                        presence_updates=[{"character_id": "rina", "action": "leave"}]
                    ),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "POV_LEAVE_INVALID"
        assert storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})["turn_number"] == 0
