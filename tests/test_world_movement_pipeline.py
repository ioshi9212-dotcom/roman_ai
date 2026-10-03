import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import storage
from app import turn_pipeline as pipeline


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _soft_turn(number: int):
    return {
        "turn_number": number,
        "scene_output": "камерная бытовая сцена продолжается",
        "extracted": {
            "scene_progressed": True,
            "chronology": [],
            "relationship_updates": [{
                "character_id": "silas",
                "dimensions": [{"label": "близость", "delta": 1}],
            }],
            "npc_intent_updates": [{
                "character_id": "silas",
                "intent_id": f"pending_{number}",
                "summary": "Продолжить разговор позже",
            }],
            "story_thread_updates": [],
            "presence_updates": [],
            "state_patch": {},
        },
    }


def _setup_root(tmp_path: Path, monkeypatch):
    sessions = tmp_path / "sessions"
    root = sessions / "sid"
    root.mkdir(parents=True)
    monkeypatch.setattr(storage, "SESSIONS_DIR", sessions)
    _write_json(
        root / "state.json",
        {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "дом Сайласа",
                "location_id": "silas_house",
                "time": "08:00",
                "present_characters": ["rina", "silas"],
            },
            "characters": {
                "rayna": {"location": "дом Сайласа", "location_id": "silas_house"},
                "leon": {"location": "город"},
            },
        },
    )
    return root


def test_soft_relationship_and_pending_intent_do_not_count_as_world_movement(tmp_path, monkeypatch):
    root = _setup_root(tmp_path, monkeypatch)
    turns = [_soft_turn(i) for i in range(1, pipeline.WORLD_STAGNATION_LIMIT + 1)]
    (root / "turns.jsonl").write_text(
        "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
        encoding="utf-8",
    )

    assert pipeline._trailing_world_stagnant_turns(root) == pipeline.WORLD_STAGNATION_LIMIT

    payload = {
        "extracted": {
            "scene_progressed": True,
            "chronology": [],
            "relationship_updates": [{
                "character_id": "silas",
                "dimensions": [{"label": "близость", "delta": 1}],
            }],
            "npc_intent_updates": [{
                "character_id": "silas",
                "intent_id": "still_pending",
                "summary": "Продолжить разговор позже",
            }],
            "story_thread_updates": [],
            "presence_updates": [],
            "state_patch": {},
        }
    }

    with pytest.raises(HTTPException) as exc:
        pipeline._validate_world_movement("sid", payload)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "STORY_PROGRESS_REQUIRED"
    assert exc.value.detail["world_static_turns_before_this_commit"] == pipeline.WORLD_STAGNATION_LIMIT


def test_real_arrival_or_large_time_transition_satisfies_world_movement_gate(tmp_path, monkeypatch):
    root = _setup_root(tmp_path, monkeypatch)
    turns = [_soft_turn(i) for i in range(1, pipeline.WORLD_STAGNATION_LIMIT + 1)]
    (root / "turns.jsonl").write_text(
        "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
        encoding="utf-8",
    )

    pipeline._validate_world_movement(
        "sid",
        {
            "extracted": {
                "chronology": [],
                "story_thread_updates": [],
                "npc_intent_updates": [],
                "presence_updates": [{"character_id": "rayna", "action": "enter"}],
                "state_patch": {},
            }
        },
    )

    pipeline._validate_world_movement(
        "sid",
        {
            "extracted": {
                "chronology": [],
                "story_thread_updates": [],
                "npc_intent_updates": [],
                "presence_updates": [],
                "state_patch": {"current": {"time": "11:30"}},
            }
        },
    )


def test_world_movement_context_prioritizes_current_location_people(tmp_path, monkeypatch):
    root = _setup_root(tmp_path, monkeypatch)
    (root / "turns.jsonl").write_text("", encoding="utf-8")
    state = storage._read_json(root / "state.json", {})
    location_context = {
        "location_id": "silas_house",
        "name": "дом Сайласа",
        "linked_characters": [
            {"character_id": "rayna", "relation": "связана с домом"},
            {"character_id": "leon", "relation": "часто бывает здесь"},
        ],
    }

    context = pipeline._world_movement_context(root, state, location_context)

    assert "rayna" in context["location_linked_character_ids"]
    assert "leon" in context["location_linked_character_ids"]
    assert "rayna" in context["same_parent_location_character_ids"]
    assert context["director_only"] is True
