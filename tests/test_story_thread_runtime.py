import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from app import storage
from app import story_thread
from app import story_thread_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _static_turn(number: int):
    return {
        "turn_number": number,
        "user_input": "(заняться своими делами)",
        "scene_output": "тихий бытовой ход",
        "extracted": {
            "chronology": [],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
        },
    }


def test_story_thread_lifecycle_preserves_anchor_facts_and_progress_turn():
    state = {"threads": {}}
    state = story_thread.apply_updates(
        state,
        [{
            "thread_id": "war-op",
            "operation": "upsert",
            "premise": "операция началась",
            "current_phase": "подготовка",
            "anchor_facts": ["цель известна"],
            "unresolved": ["кто внутри"],
            "progressed_now": True,
        }],
        current_turn=10,
    )
    thread = state["threads"]["war-op"]
    assert thread["last_progress_turn"] == 10
    assert thread["progress_count"] == 1

    state = story_thread.apply_updates(
        state,
        [{
            "thread_id": "war-op",
            "operation": "upsert",
            "current_phase": "вход",
            "anchor_facts": ["вход заминирован"],
            "unresolved": ["обойти мину"],
            "progressed_now": True,
        }],
        current_turn=12,
    )
    thread = state["threads"]["war-op"]
    assert thread["anchor_facts"] == ["цель известна", "вход заминирован"]
    assert thread["current_phase"] == "вход"
    assert thread["unresolved"] == ["обойти мину"]
    assert thread["last_progress_turn"] == 12

    state = story_thread.apply_updates(
        state,
        [{"thread_id": "war-op", "operation": "resolve", "resolution": "цель выведена"}],
        current_turn=15,
    )
    assert state["threads"]["war-op"]["status"] == "resolved"
    assert state["threads"]["war-op"]["resolved_turn"] == 15


def test_fourth_consecutive_static_commit_is_rejected_but_real_progress_passes(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        root = sessions / "sid"
        root.mkdir(parents=True)
        monkeypatch.setattr(storage, "SESSIONS_DIR", sessions)
        _write_json(root / "state.json", {"threads": {}})
        _write_json(root / "meta.json", {"turn_number": 3})
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(_static_turn(i), ensure_ascii=False) for i in range(1, 4)) + "\n",
            encoding="utf-8",
        )

        payload = {
            "scene_output": "ещё один статичный ход",
            "extracted": {
                "chronology": [],
                "npc_intent_updates": [],
                "story_thread_updates": [],
            },
        }
        with pytest.raises(HTTPException) as exc:
            runtime._with_story_patch("sid", payload, audit=False)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "STORY_PROGRESS_REQUIRED"
        assert exc.value.detail["stagnant_turns_before_this_commit"] == 3

        moving = {
            "scene_output": "событие сдвинуло операцию",
            "extracted": {
                "chronology": [{"event": "пришёл приказ на выезд"}],
                "npc_intent_updates": [],
                "story_thread_updates": [{
                    "thread_id": "deployment",
                    "operation": "upsert",
                    "premise": "приказ на выезд",
                    "current_phase": "сбор",
                    "progressed_now": True,
                }],
            },
        }
        prepared = runtime._with_story_patch("sid", moving, audit=False)
        assert prepared["extracted"]["state_patch"]["threads"]["deployment"]["last_progress_turn"] == 4


def test_player_action_or_continuous_important_scene_can_mark_progress_without_fake_canon(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        root = sessions / "sid"
        root.mkdir(parents=True)
        monkeypatch.setattr(storage, "SESSIONS_DIR", sessions)
        _write_json(root / "state.json", {"threads": {}})
        _write_json(root / "meta.json", {"turn_number": 3})
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(_static_turn(i), ensure_ascii=False) for i in range(1, 4)) + "\n",
            encoding="utf-8",
        )

        payload = {
            "scene_output": "явное действие игрока естественно завершилось в текущем моменте",
            "extracted": {
                "scene_progressed": True,
                "chronology": [],
                "npc_intent_updates": [],
                "story_thread_updates": [],
            },
        }
        prepared = runtime._with_story_patch("sid", payload, audit=False)
        assert prepared["extracted"]["scene_progressed"] is True
        assert prepared["extracted"]["chronology"] == []


def test_story_pressure_starts_early_and_becomes_mandatory_at_six_turns():
    context = {
        "active_threads": {
            "medium": {"summary": "операция", "priority": "medium", "last_progress_turn": 10},
            "fresh": {"summary": "разговор", "priority": "medium", "last_progress_turn": 14},
        }
    }
    pressure = runtime._story_pressure(context, current_turn=16)
    assert [row["thread_id"] for row in pressure] == ["medium"]
    assert pressure[0]["turns_since_progress"] == 6
    assert pressure[0]["must_advance_or_causally_pause"] is True


def test_story_drive_forces_movement_but_keeps_meaningful_pov_choice_with_player(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sid"
        root.mkdir(parents=True)
        _write_json(root / "state.json", {"threads": {}})
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(_static_turn(i), ensure_ascii=False) for i in range(1, 4)) + "\n",
            encoding="utf-8",
        )
        drive = runtime._story_drive(
            {"future_guidance": {"story_direction": {"war": "конфликт должен выйти на поверхность"}}},
            root,
            current_turn=3,
            pressure=[],
        )
        assert drive["mandatory"] is True
        assert drive["stagnant_turns"] == 3
        assert drive["force_progress_this_turn"] is True
        assert drive["future_direction_cues"]
        assert "scene_progressed=true" in drive["scene_progress_flag"]
        assert "next meaningful POV choice" in drive["instruction"]
        assert "compress the uneventful part" in drive["instruction"]
        assert "consent" in drive["instruction"]


def test_actions_schema_and_gpt_instruction_include_story_thread_contract():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert schema["info"]["version"] == "1.14.0"
    extracted = schema["components"]["schemas"]["TurnCommit"]["properties"]["extracted"]
    assert "story_thread_updates" in extracted["required"]
    assert extracted["properties"]["story_thread_updates"]["items"]["$ref"].endswith("StoryThreadUpdate")
    assert extracted["properties"]["scene_progressed"]["type"] == "boolean"

    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "narrative_guardrails" in instructions
    assert "story_drive" in instructions
    assert "STORY_PROGRESS_REQUIRED" in instructions
    assert "story_thread_updates" in instructions
    assert "scene_progressed=true" in instructions
    assert "следующего значимого выбора" in instructions
    assert "Быстро проверить" not in instructions


def test_scene_builder_is_not_modified_by_story_engine_fix():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert text.startswith("Формат scene_builder обязателен")
    assert "2000–3000 символов" in text
