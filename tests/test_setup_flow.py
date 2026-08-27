import json
import tempfile
from pathlib import Path

from app import storage
from app.novel_access import get_novel_read_chunk, prepare_novel_read
from app.session_preview import get_session_preview
from tests.helpers import commit_with_packet


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_setup_can_verify_saved_canon_preview_real_session_and_launch_first_scene():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "setup_test",
            "title": "Тишина между мирами",
            "version": 1,
            "novel": {"pov_character": "elena", "genre": "romance"},
            "rules": {"pov_control": "user"},
            "lore": {"public": "East and West are divided"},
            "hidden_lore": {"secret": "archive"},
            "story_direction": {"focus": "relationships"},
            "characters": [
                {"character_id": "elena", "name": "Елена", "is_pov": True, "role": "POV"},
                {"character_id": "aiden", "name": "Эйден", "role": "commander"},
            ],
            "starting_state": {
                "current": {
                    "date": "03.09.1451",
                    "time": "09:10",
                    "location": "Восточный сектор",
                    "scene": "прибытие",
                    "present_characters": ["elena", "aiden"],
                }
            },
        }
        storage.save_novel(novel)

        read = prepare_novel_read("setup_test")
        full = ""
        for index in range(read["chunk_count"]):
            full += get_novel_read_chunk(read["read_id"], index)["content"]
        restored = json.loads(full)
        assert restored["title"] == "Тишина между мирами"
        assert restored["hidden_lore"]["secret"] == "archive"
        assert len(restored["characters"]) == 2

        meta = storage.create_session(novel)
        sid = meta["session_id"]
        assert sid

        preview = get_session_preview(sid)
        assert preview["session_id"] == sid
        assert preview["pov"]["name"] == "Елена"
        assert preview["start"]["location"] == "Восточный сектор"
        assert "Эйден" in preview["start"]["present_characters"]
        assert preview["turn_number"] == 0

        result = commit_with_packet(
            sid,
            "запускай первую сцену",
            "Первая сцена открыта без отдельного игрового действия POV.",
            {"chronology": [{"turn": 1, "summary": "История началась"}]},
        )
        assert result["turn_number"] == 1
