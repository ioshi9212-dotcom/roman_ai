import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import cast_registry_runtime, scene_presence_runtime, storage, turn_pipeline


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def make_session() -> str:
    novel = {
        "novel_id": "persistent-npc-gate",
        "title": "Persistent NPC Gate",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "Кайр", "is_pov": True},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "05.10.2026",
                "time": "20:00",
                "location": "bar",
                "present_characters": ["pov"],
            },
        },
    }
    return storage.create_session(novel)["session_id"]


def put_unknown_ada_in_persistent_presence(sid: str):
    root = storage.SESSIONS_DIR / sid
    state = storage._read_json(root / "state.json", {})
    state["current"]["present_characters"] = ["pov", "Ада"]
    storage._write_json(root / "state.json", state)


def test_unknown_named_npc_cannot_persist_across_turns_without_card():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        payload = {
            "user_input": "(остаться рядом)",
            "scene_output": "Ада остаётся рядом.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [],
                "presence_updates": [],
                "state_patch": {},
            },
        }

        with pytest.raises(RuntimeError, match="CAST_PERSISTENT_NPC_CARD_REQUIRED"):
            cast_registry_runtime._with_registry_patch(sid, payload)


def test_persistent_named_npc_passes_after_character_upsert():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        payload = {
            "user_input": "(остаться рядом)",
            "scene_output": "Ада остаётся рядом.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [{
                    "character_id": "ada",
                    "name": "Ада",
                    "story_function": "устойчивый участник текущей линии",
                }],
                "presence_updates": [],
                "state_patch": {},
            },
        }

        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        patch = prepared["extracted"]["state_patch"]["world"]["cast_registry"]
        assert patch["ada"]["origin"] == "story_created"
        assert patch["ada"]["name"] == "Ада"


def test_unregistered_one_off_extra_can_leave_without_becoming_a_card():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)
        root = storage.SESSIONS_DIR / sid

        payload = {
            "extracted": {
                "presence_updates": [{"character_id": "Ада", "action": "leave"}],
                "state_patch": {},
            },
        }
        prepared = scene_presence_runtime._apply_presence_contract(payload, root=root)
        assert prepared["extracted"]["state_patch"]["current"]["present_characters"] == ["pov"]


def test_participating_story_npc_needs_first_relationship_dimension():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        base = {
            "user_input": "(ответить Аде)",
            "scene_output": "Кайр отвечает Аде.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [{
                    "character_id": "ada",
                    "name": "Ада",
                    "story_function": "устойчивый участник текущей линии",
                }],
                "relationship_updates": [],
                "state_patch": {},
            },
        }

        with pytest.raises(HTTPException) as exc:
            turn_pipeline._apply_relationship_changes(sid, base)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_INITIALIZATION_REQUIRED"

        base["extracted"]["relationship_updates"] = [{
            "character_id": "ada",
            "reason": "содержательное взаимодействие с POV",
            "dimensions": [{"label": "интерес", "value": 1}],
        }]
        prepared = turn_pipeline._apply_relationship_changes(sid, base)
        relation = prepared["_relationships_after"]["npc_to_pov"]["ada"]
        assert relation["dimensions"]["интерес"]["value"] == 1
