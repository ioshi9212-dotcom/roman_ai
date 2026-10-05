import tempfile
from pathlib import Path

import pytest

from app import storage
from app.operation_service import commit_turn_request, prepare_turn_request
from app.scene_presence_runtime import _apply_presence_contract
from app.stability_runtime import _merge_state_patch_exact_relationships
from app.turn_context import _scene_character_ids


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _cards():
    return [
        {"character_id": "pov", "name": "Рината", "is_pov": True},
        {"character_id": "silas", "name": "Сайлас"},
        {"character_id": "noah", "name": "Ной"},
    ]


def test_remote_call_is_scene_participation_without_physical_presence():
    state = {
        "current": {
            "present_characters": ["pov"],
            "remote_characters": ["silas"],
        },
        "pov": {"character_id": "pov"},
        "characters": {},
    }
    ids = _scene_character_ids({}, state, _cards())
    assert ids == ["pov", "silas"]
    assert storage._present_character_ids(state) == ["pov"]
    assert storage._remote_character_ids(state) == ["silas"]
    assert storage._scene_participant_ids(state) == ["pov", "silas"]



def test_explicit_current_roster_ignores_stale_runtime_present_flag():
    state = {
        "current": {"present_characters": ["pov"]},
        "pov": {"character_id": "pov"},
        "characters": {
            "noah": {"present": True, "location": "old room"},
        },
    }
    assert storage._present_character_ids(state) == ["pov"]


def test_physical_entry_removes_same_character_from_remote_roster():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"characters": _cards()})
        storage._write_json(root / "characters.json", _cards())
        storage._write_json(
            root / "state.json",
            {
                "current": {
                    "present_characters": ["pov"],
                    "remote_characters": ["silas"],
                    "remote_channels": {"silas": "звонок"},
                    "positions": {},
                },
                "pov": {"character_id": "pov"},
                "characters": {},
            },
        )

        payload = {
            "extracted": {
                "presence_updates": [
                    {"character_id": "silas", "action": "enter", "zone": "прихожая"}
                ],
                "state_patch": {},
            }
        }
        prepared = _apply_presence_contract(payload, root=root)
        current = prepared["extracted"]["state_patch"]["current"]
        assert current["present_characters"] == ["pov", "silas"]
        assert current["remote_characters"] == []
        assert current["positions"]["silas"]["zone"] == "прихожая"


def test_remote_roster_can_be_ended_without_physical_leave():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"characters": _cards()})
        storage._write_json(root / "characters.json", _cards())
        storage._write_json(
            root / "state.json",
            {
                "current": {
                    "present_characters": ["pov"],
                    "remote_characters": ["silas"],
                    "remote_channels": {"silas": "сообщения"},
                    "positions": {},
                },
                "pov": {"character_id": "pov"},
                "characters": {},
            },
        )

        payload = {
            "extracted": {
                "presence_updates": [],
                "state_patch": {"current": {"remote_characters": []}},
            }
        }
        prepared = _apply_presence_contract(payload, root=root)
        current = prepared["extracted"]["state_patch"]["current"]
        assert current["remote_characters"] == []
        assert "present_characters" not in current


def test_scene_items_patch_replaces_old_snapshot_instead_of_leaving_ghost_location():
    state = {
        "current": {
            "scene_items": {
                "lighter": {"location": "стол"},
                "phone": {"holder": "pov"},
            }
        }
    }
    updated = _merge_state_patch_exact_relationships(
        state,
        {
            "current": {
                "scene_items": {
                    "lighter": {"holder": "pov"},
                }
            }
        },
    )
    assert updated["current"]["scene_items"] == {
        "lighter": {"holder": "pov"}
    }


def _continuity_session(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    return storage.create_session({
        "novel_id": "continuity",
        "title": "Continuity",
        "version": 5,
        "novel": {"pov_character": "pov"},
        "characters": _cards(),
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026", "time": "10:00", "location": "кухня",
                "present_characters": ["pov", "silas"],
                "positions": {"silas": {"zone": "у стола"}},
            },
        },
    })["session_id"]


def _continuity_payload(sid, current_patch, *, header=True, presence_updates=None):
    user_input = "(продолжить)"
    manifest = prepare_turn_request(sid, user_input, request_id="continuity-1")
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    scene = "POV продолжает сцену."
    if header:
        scene = "🎭 Continuity · осень\n🕒 День 1 · вторник, 01.09.2026, 10:05 · 📍 кухня 🌦️ Погода: ясно\n" + scene
    return {
        "packet_id": manifest["packet_id"], "user_input": user_input,
        "scene_output": scene,
        "extracted": {
            "scene_builder_reviewed": True, "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "state_patch": {"current": current_patch},
            "presence_updates": presence_updates or [],
        },
    }


@pytest.mark.parametrize("patch,header,expected", [
    ({"time": "10:15", "location": "кабинет"}, True, ("01.09.2026", "10:15", "кабинет", 1)),
    ({"time": "10:15"}, True, ("01.09.2026", "10:15", "кухня", 1)),
    ({}, True, ("01.09.2026", "10:05", "кухня", 1)),
    ({}, False, ("01.09.2026", "10:00", "кухня", 1)),
    ({"date": "02.09.2026", "time": "00:10"}, True, ("02.09.2026", "00:10", "кухня", 2)),
    ({"time": "10:00", "location": "кухня"}, True, ("01.09.2026", "10:00", "кухня", 1)),
])
def test_commit_keeps_explicit_final_pointer_and_header_fallback(tmp_path, monkeypatch, patch, header, expected):
    sid = _continuity_session(tmp_path, monkeypatch)
    payload = _continuity_payload(sid, patch, header=header)
    commit_turn_request(sid, payload)
    current = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})["current"]
    assert (current["date"], current["time"], current["location"], current["game_day"]) == expected
    assert current["present_characters"] == ["pov", "silas"]


def test_scene_transition_retry_and_rollback_preserve_exact_state(tmp_path, monkeypatch):
    from app.turn_rollback import rollback_last_turn

    sid = _continuity_session(tmp_path, monkeypatch)
    root = storage.SESSIONS_DIR / sid
    payload = _continuity_payload(sid, {
        "time": "10:15", "location": "кабинет", "present_characters": ["pov"],
    }, presence_updates=[{"character_id": "silas", "action": "leave"}])
    before = storage._read_json(root / "state.json", {})
    payload["scene_output"] += " POV вошла в кабинет. Сайлас остался на кухне."
    commit_turn_request(sid, payload)
    after = storage._read_json(root / "state.json", {})
    assert after["current"]["location"] == "кабинет"
    assert after["current"]["time"] == "10:15"
    assert after["current"]["present_characters"] == ["pov"]
    assert after["current"]["left_characters"] == ["silas"]
    assert "silas" not in after["current"]["positions"]

    commit_turn_request(sid, payload)
    assert storage._read_json(root / "state.json", {}) == after
    assert len(storage._read_turns(root)) == 1
    rollback_last_turn(sid, expected_turn_number=1, confirm=True)
    assert storage._read_json(root / "state.json", {}) == before


def test_roster_omission_does_not_remove_npc_without_leave(tmp_path, monkeypatch):
    sid = _continuity_session(tmp_path, monkeypatch)
    payload = _continuity_payload(sid, {"present_characters": ["pov"]})
    commit_turn_request(sid, payload)
    current = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})["current"]
    assert current["present_characters"] == ["pov", "silas"]
    assert current["left_characters"] == []
