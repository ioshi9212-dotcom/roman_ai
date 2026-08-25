import tempfile
from pathlib import Path

from app import storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_sixty_turn_handoff_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)

        novel = {
            "novel_id": "test_novel",
            "title": "Test Novel",
            "version": 1,
            "novel": {},
            "characters": [],
            "lore": {},
        }
        storage.save_novel(novel)
        meta = storage.create_session(novel)
        session_id = meta["session_id"]

        for turn in range(1, 61):
            result = storage.commit_turn(
                session_id,
                {
                    "user_input": f"user {turn}",
                    "scene_output": f"scene {turn}",
                    "extracted": {
                        "state_patch": {"current": {"last_turn": turn}},
                        "chronology": [{"turn": turn, "summary": f"event {turn}"}],
                    },
                },
            )

        assert result["turn_number"] == 60
        assert result["audit_due"] is True
        assert result["handoff_required"] is True
        assert result["audit_range"] == [46, 60]

        tail = storage.get_turn_range(session_id, 55, 60)
        assert [t["turn_number"] for t in tail] == [55, 56, 57, 58, 59, 60]

        package = storage.build_resume_package(session_id)
        assert package["meta"]["turn_number"] == 60
        assert [t["turn_number"] for t in package["handoff_tail"]] == [55, 56, 57, 58, 59, 60]
        assert package["state"]["current"]["last_turn"] == 60
        assert len(package["chronology"]) == 60

        try:
            storage.commit_turn(
                session_id,
                {"user_input": "should fail", "scene_output": "should fail", "extracted": {}},
            )
            assert False, "turn 61 must be blocked before resume"
        except RuntimeError as exc:
            assert str(exc) == "HANDOFF_REQUIRED"

        confirmed = storage.confirm_resume(session_id, package["resume_token"])
        assert confirmed["turn_number"] == 60
        assert confirmed["handoff_generation"] == 1

        next_turn = storage.commit_turn(
            session_id,
            {"user_input": "user 61", "scene_output": "scene 61", "extracted": {}},
        )
        assert next_turn["turn_number"] == 61

        root = storage.SESSIONS_DIR / session_id
        assert not (root / "handoff_tail.json").exists()
        assert not (root / "resume_token.json").exists()
        assert len(storage.get_turn_range(session_id, 1, 61)) == 61
