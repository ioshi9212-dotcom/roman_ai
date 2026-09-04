import tempfile
from pathlib import Path

from app import storage
from app.context_stats import session_context_stats


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_context_stats_is_read_only_and_reports_memory_and_chronology():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "diag",
            "title": "Diagnostics",
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {
                    "date": "02.09.2026",
                    "time": "10:00",
                    "location": "room",
                    "present_characters": ["pov", "npc"],
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._read_json(root / "memory.json", {})
        memory.setdefault("characters", {})["npc"] = {
            "knowledge": [{"fact_id": "f1", "fact": "x"}],
            "experiences": [{"event_id": "e1", "event": "y"}],
            "dialogue_memory": [{"topic_id": "t1", "summary": "z"}],
        }
        storage._write_json(root / "memory.json", memory)
        storage._write_json(root / "chronology.json", [
            {"event_id": "c1", "turn_number": 1, "event": "event one", "importance": "major"},
            {"event_id": "c2", "turn_number": 2, "event": "event two", "importance": "normal"},
        ])

        before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        stats = session_context_stats(sid)
        after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

        assert stats["read_only"] is True
        assert stats["chronology"]["events"] == 2
        assert stats["chronology"]["importance_counts"] == {"major": 1, "normal": 1}
        assert stats["memory"]["by_character"]["npc"]["knowledge"] == 1
        assert stats["memory"]["by_character"]["npc"]["experiences"] == 1
        assert stats["memory"]["by_character"]["npc"]["dialogue_memory"] == 1
        assert before == after
