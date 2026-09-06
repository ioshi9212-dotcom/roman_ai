import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.rollback_diagnostics import rollback_diagnostics


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_rollback_diagnostics_is_read_only_and_reports_last_turn_structure():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "rollback_diag",
            "title": "Rollback Diagnostics",
            "novel": {"pov_character": "pov"},
            "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {
                    "date": "01.09.2026",
                    "time": "10:00",
                    "location": "room",
                    "present_characters": ["pov"],
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        manifest = session_runtime.prepare_turn_packet(sid, "Ход.")
        for index in range(manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "Ход.",
                "scene_output": "🎭 Test · осень\n🕒 День 1 · вторник, 01.09.2026, 10:01 · 📍 room\n\nСцена.\n\nСостояние: нормально\nОтношения:\n\nХод 1",
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "state_patch": {"current": {"time": "10:01"}},
                },
            },
        )
        before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        result = rollback_diagnostics(sid)
        after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

        assert result["read_only"] is True
        assert result["current_turn"] == 1
        assert result["last_turn_extracted"]["state_patch_top_level_keys"] == ["current"]
        assert "current.time" in result["last_turn_extracted"]["state_patch_leaf_paths"]
        assert before == after
