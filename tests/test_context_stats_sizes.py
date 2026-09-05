import tempfile
from pathlib import Path

from app import storage
from app.context_stats import session_context_stats


def test_context_stats_reports_file_sizes():
    with tempfile.TemporaryDirectory() as tmp:
        storage.DATA_DIR = Path(tmp)
        storage.LIBRARY_DIR = storage.DATA_DIR / "library"
        storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
        storage.ensure_dirs()
        novel = {
            "novel_id": "diag_sizes",
            "title": "Diagnostics sizes",
            "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {
                    "date": "02.09.2026",
                    "time": "10:00",
                    "location": "room",
                    "present_characters": ["pov"],
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        stats = session_context_stats(sid)
        assert stats["files_bytes"]["source.json"] > 0
        assert stats["files_bytes"]["state.json"] > 0
        assert stats["known_session_bytes"] >= stats["files_bytes"]["source.json"]
