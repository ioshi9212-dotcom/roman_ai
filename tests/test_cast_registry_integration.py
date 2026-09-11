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


def test_story_created_rotation_age_starts_at_registration_not_turn_zero():
    cards = [
        {"character_id": "pov", "name": "POV", "is_pov": True},
        {"character_id": "mark", "name": "Марк", "role": "doctor"},
    ]
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"]},
        "relationships": {},
        "world": {"cast_registry": {
            "mark": {
                "character_id": "mark",
                "name": "Марк",
                "origin": "story_created",
                "status": "active",
                "first_registered_turn": 12,
                "appearance_count": 0,
            }
        }},
    }
    source_ids = {"pov"}
    assert cast_registry_runtime._rotation_pressure(
        state, cards, current_turn=20, source_character_ids=source_ids
    ) == []
    pressure = cast_registry_runtime._rotation_pressure(
        state, cards, current_turn=27, source_character_ids=source_ids
    )
    row = next(item for item in pressure if item["character_id"] == "mark")
    assert row["turns_since_activity"] == 15
    assert row["turns_since_appearance"] == 15


def test_legacy_dynamic_card_bootstraps_as_story_created_before_first_registry_commit():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        cards = storage._read_json(root / "characters.json", [])
        cards.append({"character_id": "mark", "name": "Марк", "role": "doctor"})
        storage._write_json(root / "characters.json", cards)
        source = storage._read_json(root / "source.json", {})
        source_ids = {storage._card_id(card) for card in source["characters"]}
        state = storage._read_json(root / "state.json", {})

        registry = cast_registry_runtime._ensure_registry(
            state, cards, current_turn=20, source_character_ids=source_ids
        )
        assert registry["liam"]["origin"] == "player_created"
        assert registry["mark"]["origin"] == "story_created"


def test_registry_uses_canonical_presence_contract_for_direct_roster_patch():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        payload = {
            "user_input": "Лиам входит.",
            "scene_output": "Лиам вошёл.",
            "extracted": {
                "chronology": [],
                "state_patch": {"current": {"present_characters": ["pov", "liam"]}},
            },
        }
        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        current_patch = prepared["extracted"]["state_patch"]["current"]
        assert current_patch["present_characters"] == ["pov", "liam"]
        assert "liam" in current_patch["entered_characters"]
        registry = prepared["extracted"]["state_patch"]["world"]["cast_registry"]
        assert registry["liam"]["last_appearance_turn"] == 1
        assert registry["liam"]["last_contact_turn"] == 1


def test_existing_registry_persists_only_changed_rows_on_next_turn():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        first = cast_registry_runtime._with_registry_patch(sid, {
            "user_input": "first",
            "scene_output": "first",
            "extracted": {"chronology": []},
        })
        state = storage._read_json(root / "state.json", {})
        state.setdefault("world", {})["cast_registry"] = first["extracted"]["state_patch"]["world"]["cast_registry"]
        storage._write_json(root / "state.json", state)
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 1
        storage._write_json(root / "meta.json", meta)

        second = cast_registry_runtime._with_registry_patch(sid, {
            "user_input": "second",
            "scene_output": "second",
            "extracted": {"chronology": []},
        })
        delta = second["extracted"]["state_patch"]["world"]["cast_registry"]
        assert set(delta) == {"pov"}
        assert delta["pov"]["last_appearance_turn"] == 2


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
