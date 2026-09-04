import tempfile
from copy import deepcopy
from pathlib import Path

from app import storage
from app.context_stats import session_context_stats


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_context_stats_reports_sizes_without_mutating_session():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "stats",
            "title": "Stats",
            "novel": {"pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Rina", "is_pov": True},
                {"character_id": "aiden", "name": "Aiden"},
            ],
            "starting_state": {
                "pov": {"character_id": "rina"},
                "current": {"location": "room", "present_characters": ["rina", "aiden"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._read_json(root / "memory.json", {})
        memory.setdefault("characters", {})["aiden"] = {
            "knowledge": [{"fact": "x"}],
            "experiences": [{"event": "y"}],
            "dialogue_memory": [{"line": "z"}],
        }
        storage._write_json(root / "memory.json", memory)
        storage._write_json(
            root / "chronology.json",
            [{"event_id": "e1", "turn_number": 1, "event": "Something happened", "importance": "major"}],
        )

        before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
        result = session_context_stats(sid)
        after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}

        assert result["read_only"] is True
        assert result["chronology"]["events"] == 1
        assert result["chronology"]["importance_counts"] == {"major": 1}
        assert result["memory"]["by_character"]["aiden"]["knowledge"] == 1
        assert result["memory"]["by_character"]["aiden"]["experiences"] == 1
        assert result["memory"]["by_character"]["aiden"]["dialogue_memory"] == 1
        assert before == after
