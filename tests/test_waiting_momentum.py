import json

import pytest

from app import session_runtime, storage
from app.turn_pipeline import _wait_input, _waiting_momentum_context


@pytest.mark.parametrize("raw", [
    "(ждать Данте)",
    "(продолжать ждать Дантэ)",
    "(подождать у двери)",
    "(сидеть и ожидать возвращения)",
])
def test_wait_input_detects_passive_wait(raw):
    assert _wait_input(raw) is True


@pytest.mark.parametrize("raw", [
    "(не ждать Данте)",
    "(пойти встретить Данте)",
    "(поцеловать Данте)",
    "Я буду ждать тебя.",
    "Я жду тебя.",
    "(ждать реакции Данте)",
    "(ждать ответа)",
])
def test_wait_input_does_not_force_active_interactions(raw):
    assert _wait_input(raw) is False


def test_repeated_wait_count_has_no_forced_spawn_or_hard_gate():
    prior = [{"user_input": "(ждать Данте)"} for _ in range(4)]
    cue = _waiting_momentum_context("(продолжать ждать Данте)", prior)
    assert cue["kind"] == "passive_wait"
    assert cue["consecutive_wait_inputs"] == 5
    assert set(cue) == {"kind", "consecutive_wait_inputs"}
    assert _waiting_momentum_context("(обнять Данте)", prior) is None
    assert _waiting_momentum_context("(ждать Данте)", [
        {"user_input": "(ждать Данте)"},
        {"user_input": "(спросить дорогу)"},
    ])["consecutive_wait_inputs"] == 1


def test_active_prepare_packet_contains_waiting_data_without_directing_prose(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    novel = {
        "novel_id": "waiting-test",
        "title": "Waiting Test",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "pov", "genres": ["romance"]},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "dante", "name": "Дантэ", "goals": ["вернуться домой"]},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026",
                "time": "20:00",
                "location": "room",
                "present_characters": ["pov"],
            },
        },
    }
    sid = storage.create_session(novel)["session_id"]
    manifest = session_runtime.prepare_turn_packet(sid, "(ждать Дантэ)")
    parts = [manifest["content"]]
    for i in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], i)["content"])
    ctx = json.loads("".join(parts))
    cue = ctx["waiting_momentum"]
    assert cue["consecutive_wait_inputs"] == 1
    assert set(cue) == {"kind", "consecutive_wait_inputs"}
    assert "Если POV выбрал ждать" in ctx["scene_builder"]
    assert "story_drive" not in ctx
    assert "story_pressure" not in ctx
    assert "scene_builder" in ctx
