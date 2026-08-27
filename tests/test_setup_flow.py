import json
import tempfile
from pathlib import Path

from app import storage
from app.novel_access import get_novel_read_chunk
from app.novel_drafts import (
    create_draft,
    create_session_from_draft,
    finalize_draft,
    prepare_draft_read,
    publish_draft_to_library,
    save_section,
)
from app.session_preview import get_session_preview
from tests.helpers import commit_with_packet


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_new_chat_novel_stays_out_of_library_until_explicit_publish():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)

        draft = create_draft("setup_test", "Тишина между мирами", 1)
        draft_id = draft["draft_id"]
        sections = {
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
        for name, value in sections.items():
            save_section(draft_id, name, json.dumps(value, ensure_ascii=False))

        finalized = finalize_draft(draft_id)
        assert finalized["ok"] is True
        assert finalized["saved_to_library"] is False
        assert storage.list_novels() == []

        read = prepare_draft_read(draft_id)
        full = ""
        for index in range(read["chunk_count"]):
            full += get_novel_read_chunk(read["read_id"], index)["content"]
        restored = json.loads(full)
        assert restored["title"] == "Тишина между мирами"
        assert restored["hidden_lore"]["secret"] == "archive"
        assert len(restored["characters"]) == 2

        meta = create_session_from_draft(draft_id)
        sid = meta["session_id"]
        assert sid
        assert storage.list_novels() == []

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

        published = publish_draft_to_library(draft_id)
        assert published["published_to_library"] is True
        assert [item["novel_id"] for item in storage.list_novels()] == ["setup_test"]


def test_session_preview_accepts_flexible_real_world_state_shapes():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "flex_preview",
            "title": "Пепел и лаванда",
            "version": 1,
            "novel": {"pov_character": "elena"},
            "characters": [
                {"character_id": "elena", "name": "Елена", "is_pov": True},
                {"character_id": "liam", "name": "Лиам"},
            ],
            "lore": {},
            "starting_state": {"current": {"location": "Восточный сектор"}},
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        state = storage._read_json(root / "state.json", {})
        state["pov"] = "Елена"
        state["current"] = {
            "game_date": "04.09.1451",
            "game_time": "10:20",
            "place": "Жилой корпус",
            "situation": "заселение",
            "present_characters": {
                "elena": {"present": True},
                "liam": {"present": True},
            },
        }
        state["threads"] = "unexpected-text-shape"
        storage._write_json(root / "state.json", state)

        preview = get_session_preview(sid)
        assert preview["session_id"] == sid
        assert preview["pov"]["character_id"] == "elena"
        assert preview["pov"]["name"] == "Елена"
        assert preview["start"]["date"] == "04.09.1451"
        assert preview["start"]["time"] == "10:20"
        assert preview["start"]["location"] == "Жилой корпус"
        assert preview["start"]["scene"] == "заселение"
        assert preview["start"]["present_characters"] == ["Елена", "Лиам"]
        assert preview["active_threads"] == {}
