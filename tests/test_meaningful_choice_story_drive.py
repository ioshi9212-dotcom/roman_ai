from pathlib import Path

from app import story_thread_runtime as runtime


def test_story_drive_uses_substantive_choice_gate_not_micro_movement(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "trailing_stagnant_turns", lambda root: 0)
    monkeypatch.setattr(runtime, "active_threads", lambda state: [])

    drive = runtime._story_drive({}, tmp_path, 42, [])
    instruction = drive["instruction"]
    progress = drive["scene_progress_flag"]

    assert "if POV chose any reasonable next action, would anything substantive change?" in instruction
    assert "If no, it is not a player choice" in instruction
    assert "Movement, observation, rechecking, routine and time passing are not beats by themselves" in instruction
    assert "risk, information, goal, relationship/emotion, conflict, consequence" in instruction

    assert "Movement, position, observation, routine execution or elapsed time alone are not progress" in progress
    assert "risk, information, objective, relationship/emotional state, conflict, consequence" in progress


def test_static_streak_error_does_not_accept_position_or_routine_as_progress():
    try:
        runtime._progress_required_error(3)
    except Exception as exc:
        detail = exc.detail
    else:
        raise AssertionError("expected progress error")

    assert detail["code"] == "STORY_PROGRESS_REQUIRED"
    assert "movement, waiting, observation, rechecking or neutral routine" in detail["message"]
    assert "position/time/routine alone" in detail["message"]
