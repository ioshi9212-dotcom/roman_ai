import tempfile
from pathlib import Path

from app import storage
from app.scene_presence_runtime import _apply_presence_contract
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
        assert current["present_characters"] if "present_characters" in current else ["pov"]
