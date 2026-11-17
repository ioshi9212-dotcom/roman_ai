"""The live pipeline must retain causal lines instead of recycling small talk."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import plot_momentum_runtime as plot
from app import story_thread


def _write_turns(root, extracted_turns):
    root.mkdir(parents=True, exist_ok=True)
    (root / "turns.jsonl").write_text(
        "".join(json.dumps({"turn_number": n, "scene_output": "бытовая сцена",
                            "extracted": body}, ensure_ascii=False) + "\n"
                for n, body in enumerate(extracted_turns, 1)),
        encoding="utf-8",
    )


def test_decorative_changes_do_not_count_as_plot_progress():
    assert not plot.has_plot_consequence({
        "scene_progressed": True,
        "relationship_updates": [{"character_id": "emily", "dimensions": []}],
        "presence_updates": [{"character_id": "visitor", "action": "enter"}],
        "npc_intent_updates": [{"character_id": "ethan", "intent_id": "i1",
                                "summary": "Поговорить позже"}],
        "story_thread_updates": [],
        "state_patch": {"current": {"location": "бар", "time": "19:00"}},
    })


def test_real_causal_result_and_durable_thread_progress_are_accepted():
    assert plot.has_plot_consequence({
        "chronology": [{"event": "Рен вернулся с новым доказательством"}]})
    assert plot.has_plot_consequence({
        "story_thread_updates": [{
            "thread_id": "lighter",
            "progressed_now": True,
            "progress_summary": "Рен обнаружил несоответствие в истории зажигалки",
        }]})
    assert plot.has_plot_consequence({
        "npc_intent_updates": [{
            "character_id": "ren", "intent_id": "search", "pursued_now": True,
            "last_outcome": "След привёл к закрытой мастерской",
        }]})


def test_bare_threads_and_unpursued_intents_are_not_fake_progress():
    assert not plot.has_plot_consequence({
        "story_thread_updates": [{"thread_id": "lighter", "operation": "upsert",
                                  "summary": "Загадка", "progressed_now": False,
                                  "current_goal": "Выяснить происхождение"}],
        "npc_intent_updates": [{"intent_id": "i", "pursued_now": False,
                                "planned_action": "Встретиться с POV"}],
    })


def test_four_barren_turns_warn_and_fifth_requires_real_plot_result(tmp_path):
    root = Path(tmp_path) / "session"
    soft = {"scene_progressed": True,
            "relationship_updates": [{"character_id": "ren"}],
            "presence_updates": [{"character_id": "mark", "action": "enter"}]}
    _write_turns(root, [soft] * 4)
    assert plot.trailing_plot_stagnant_turns(root) == 4
    with pytest.raises(HTTPException) as error:
        plot.validate_plot_momentum(root, {"extracted": soft})
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PLOT_MOMENTUM_REQUIRED"
    plot.validate_plot_momentum(root, {
        "extracted": {"chronology": [{"event": "В бар пришло письмо с угрозой"}]}})


def test_packet_pressure_has_durable_threads_and_offscreen_intents(tmp_path):
    root = Path(tmp_path) / "session"
    _write_turns(root, [{"scene_progressed": True}] * 4)
    state = {
        "current": {"game_day": 1},
        "threads": {"lighter": {
            "thread_id": "lighter", "current_phase": "поиск владельца",
            "current_goal": "Прояснить происхождение",
            "current_circumstances": "Есть зажигалка",
            "next_possible_events": ["Поиск мастерской"],
            "advance_conditions": ["Получить подтверждение"],
        }},
        "npc_intents": {"ren": [{
            "intent_id": "search", "character_id": "ren", "summary": "Искать",
            "planned_action": "Позвонить бывшему владельцу",
        }]},
    }
    packet = plot.plot_packet(root, state, {"cast_registry": {"characters": []}})
    assert packet["must_change_causal_situation_this_turn"] is True
    assert packet["saved_active_thread_count"] == 1
    assert packet["saved_active_intent_count"] == 1
    assert packet["due_npc_intents"][0]["character_id"] == "ren"
    assert packet["open_threads"][0]["advance_conditions"] == ["Получить подтверждение"]


def test_empty_state_returns_canon_drivers_without_inventing_stories(tmp_path):
    root = Path(tmp_path) / "session"
    _write_turns(root, [])
    packet = plot.plot_packet(root, {"threads": {}, "npc_intents": {}}, {
        "cast_registry": {"characters": [
            {"character_id": "emily", "is_pov": True},
            {"character_id": "ren", "goals": "Выяснить, откуда зажигалка"},
        ]},
    })
    assert packet["saved_active_thread_count"] == 0
    assert packet["saved_active_intent_count"] == 0
    assert packet["canon_character_drivers"][0]["character_id"] == "ren"


def test_story_thread_keeps_phase_next_beats_and_conditions_across_updates():
    state = story_thread.apply_updates({"threads": {}}, [{
        "thread_id": "lighter", "current_phase": "поиск", "current_goal": "Найти след",
        "participants": ["ren", "emily"],
        "current_circumstances": "Зажигалка возвращена",
        "next_possible_events": ["Выяснить место находки"],
        "advance_conditions": ["Найти свидетельство"],
        "progressed_now": True,
    }], current_turn=1)
    state = story_thread.apply_updates(state, [{
        "thread_id": "lighter", "current_phase": "проверка",
        "progressed_now": True,
    }], current_turn=2)
    saved = state["threads"]["lighter"]
    assert saved["current_phase"] == "проверка"
    assert saved["current_circumstances"] == "Зажигалка возвращена"
    assert saved["next_possible_events"] == ["Выяснить место находки"]
    assert saved["advance_conditions"] == ["Найти свидетельство"]


def test_active_turn_pipeline_calls_momentum_on_prepare_and_commit():
    source = (Path(__file__).resolve().parents[1] / "app" / "turn_pipeline.py").read_text(encoding="utf-8")
    assert "plot_momentum_runtime.plot_packet(root, state, context)" in source
    assert "plot_momentum_runtime.validate_plot_momentum(" in source
