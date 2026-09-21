from pathlib import Path

from app import story_thread_runtime as runtime


def test_story_drive_uses_substantive_choice_gate_not_micro_movement(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "trailing_stagnant_turns", lambda root: 0)
    monkeypatch.setattr(runtime, "active_threads", lambda state: [])

    drive = runtime._story_drive({}, tmp_path, 42, [])
    assert drive["mandatory"] is True
    assert drive["force_progress_this_turn"] is False
    assert "Значимый выбор POV оставляй игроку" in drive["rule"]
    assert "перемещение, ожидание и течение времени сами по себе не прогресс" in drive["scene_progress_flag"]

def test_static_streak_error_does_not_accept_position_or_routine_as_progress():
    try:
        runtime._progress_required_error(3)
    except Exception as exc:
        detail = exc.detail
    else:
        raise AssertionError("expected progress error")

    assert detail["code"] == "STORY_PROGRESS_REQUIRED"
    assert "real change" in detail["message"]
    assert "Routine/position/time alone are not progress" in detail["message"]
