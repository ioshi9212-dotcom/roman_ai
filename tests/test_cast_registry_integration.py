import tempfile
from pathlib import Path

from app import cast_registry_runtime, storage, story_thread_runtime
from app.turn_rollback import _apply_saved_turn, _initial_replay_state


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "cast_integration",
        "title": "Cast Integration",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "liam", "name": "Лиам", "role": "commander"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov"]},
        },
    }


def test_cast_registry_patch_is_technical_not_story_progress():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        payload = {
            "user_input": "(ждать)",
            "scene_output": "тихий ход",
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
                "npc_intent_updates": [],
                "story_thread_updates": [],
            },
        }
        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        registry = prepared["extracted"]["state_patch"]["world"]["cast_registry"]
        assert registry["liam"]["origin"] == "player_created"
        assert story_thread_runtime._container_has_progress(prepared["extracted"], "") is False


def test_new_named_npc_is_story_created_inside_saved_registry_patch():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        payload = {
            "user_input": "test",
            "scene_output": "test",
            "extracted": {
                "chronology": [],
                "character_upserts": [
                    {
                        "character_id": "mark",
                        "name": "Марк",
                        "role": "doctor",
                        "personality": "сухой и внимательный",
                        "goal": "выяснить причину странных травм",
                        "story_function": "medical investigator",
                    }
                ],
            },
        }
        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        registry = prepared["extracted"]["state_patch"]["world"]["cast_registry"]
        assert registry["liam"]["origin"] == "player_created"
        assert registry["mark"]["origin"] == "story_created"
        assert registry["mark"]["first_registered_turn"] == 1


def test_historical_replay_reconstructs_cast_registry_from_saved_turn_patch():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        sid = storage.create_session(novel)["session_id"]
        payload = {
            "user_input": "test",
            "scene_output": "Сцена без смены места.",
            "extracted": {
                "chronology": [{"event": "Лиам прислал сообщение", "character_ids": ["liam"]}],
                "character_upserts": [],
            },
        }
        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        turn = {
            "turn_number": 1,
            "user_input": "test",
            "scene_output": "Сцена без смены места.",
            "extracted": prepared["extracted"],
        }

        cards, state, memory, chronology = _initial_replay_state(novel)
        cards, state, memory, chronology = _apply_saved_turn(
            novel, cards, state, memory, chronology, [], turn
        )
        registry = state["world"]["cast_registry"]
        assert registry["liam"]["origin"] == "player_created"
        assert registry["liam"]["last_contact_turn"] == 1
        assert "Лиам прислал сообщение" in registry["liam"]["last_meaningful_event"]
