import tempfile
from pathlib import Path

import pytest

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
        state, cards, current_turn=48, source_character_ids=source_ids
    )
    row = next(item for item in pressure if item["character_id"] == "mark")
    assert row["turns_since_activity"] == 36
    assert row["turns_since_appearance"] == 36


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
                "dialogue_memory_add": [{
                    "topic_id": "contact",
                    "participants": ["pov", "liam"],
                    "summary": "Контакт состоялся.",
                }],
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



def test_chronology_mention_does_not_reset_rotation_contact_age():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state.setdefault("world", {})["cast_registry"] = {
            "liam": {
                "character_id": "liam",
                "name": "Лиам",
                "origin": "player_created",
                "status": "active",
                "first_registered_turn": 0,
                "last_appearance_turn": 2,
                "last_contact_turn": 2,
                "appearance_count": 1,
            }
        }
        storage._write_json(root / "state.json", state)
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 20
        storage._write_json(root / "meta.json", meta)

        prepared = cast_registry_runtime._with_registry_patch(sid, {
            "user_input": "test",
            "scene_output": "test",
            "extracted": {
                "chronology": [{"event": "POV вспомнила Лиама", "character_ids": ["liam"]}],
            },
        })
        row = prepared["extracted"]["state_patch"]["world"]["cast_registry"]["liam"]

        assert row["last_meaningful_turn"] == 21
        assert row["last_contact_turn"] == 2
        assert cast_registry_runtime._last_activity_turn(row) == 2



def test_core_cast_story_function_is_carried_into_persistent_registry():
    cards = [
        {"character_id": "pov", "name": "Елена", "is_pov": True},
        {"character_id": "liam", "name": "Лиам", "role": "рейдер"},
    ]
    source = {
        "novel": {
            "core_cast": [
                {"character_id": "pov", "name": "Елена", "story_function": "POV истории."},
                {"character_id": "liam", "name": "Лиам", "story_function": "Основная романтическая и конфликтная линия Елены."},
            ]
        },
        "characters": cards,
    }
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"], "game_day": 7},
        "world": {},
    }
    registry = cast_registry_runtime._ensure_registry(
        state,
        cards,
        current_turn=120,
        source_character_ids={"pov", "liam"},
        source=source,
    )
    assert registry["liam"]["importance"] == "core"
    assert registry["liam"]["story_function"] == "Основная романтическая и конфликтная линия Елены."
    assert registry["liam"]["card_ref"] == "liam"
    assert registry["liam"]["first_registered_game_day"] == 1


def test_rotation_pressure_balances_turns_and_game_days_and_marks_forgotten_core():
    cards = [
        {"character_id": "pov", "name": "Елена", "is_pov": True},
        {"character_id": "core", "name": "Рэй"},
    ]
    source = {
        "novel": {"core_cast": [
            {"character_id": "pov", "name": "Елена", "story_function": "POV."},
            {"character_id": "core", "name": "Рэй", "story_function": "Связывает личную линию POV с командованием базы."},
        ]},
        "characters": cards,
    }
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"], "game_day": 10},
        "relationships": {},
        "world": {"cast_registry": {
            "core": {
                "character_id": "core",
                "name": "Рэй",
                "origin": "player_created",
                "importance": "core",
                "first_registered_turn": 0,
                "first_registered_game_day": 1,
                "last_appearance_turn": 590,
                "last_appearance_game_day": 2,
                "last_contact_turn": 590,
                "last_contact_game_day": 2,
                "appearance_count": 1,
                "status": "active",
            }
        }},
    }
    pressure = cast_registry_runtime._rotation_pressure(
        state,
        cards,
        current_turn=600,
        source_character_ids={"pov", "core"},
        source=source,
    )
    row = next(item for item in pressure if item["character_id"] == "core")
    assert row["turns_since_activity"] == 10
    assert row["game_days_since_activity"] == 8
    assert row["forgotten_core"] is True
    assert "естественную" in row["return_rule"]


def test_many_turns_can_create_pressure_even_when_same_game_day():
    cards = [
        {"character_id": "pov", "name": "POV", "is_pov": True},
        {"character_id": "core", "name": "Core"},
    ]
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"], "game_day": 1},
        "world": {"cast_registry": {
            "core": {
                "character_id": "core",
                "origin": "player_created",
                "importance": "core",
                "first_registered_turn": 0,
                "first_registered_game_day": 1,
                "last_contact_turn": 1,
                "last_contact_game_day": 1,
                "last_appearance_turn": 1,
                "last_appearance_game_day": 1,
                "appearance_count": 2,
                "status": "active",
            }
        }},
    }
    pressure = cast_registry_runtime._rotation_pressure(
        state, cards, current_turn=25, source_character_ids={"pov", "core"}
    )
    row = next(item for item in pressure if item["character_id"] == "core")
    assert row["turns_since_activity"] == 24
    assert row["game_days_since_activity"] == 0


def test_story_created_upsert_requires_director_story_function():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        payload = {
            "user_input": "Новый медик вмешивается.",
            "scene_output": "test",
            "extracted": {
                "runtime_rules_reviewed": True,
                "chronology": [],
                "character_upserts": [
                    {"character_id": "new_doc", "name": "Марк", "role": "медик"}
                ],
            },
        }
        with pytest.raises(RuntimeError, match="CAST_STORY_FUNCTION_REQUIRED"):
            cast_registry_runtime._with_registry_patch(sid, payload)


def test_registry_index_keeps_every_registered_character_and_last_seen_metadata():
    registry = {
        "core": {
            "character_id": "core",
            "name": "Рэй",
            "card_ref": "core",
            "origin": "player_created",
            "importance": "core",
            "story_function": "Командная линия.",
            "status": "active",
            "last_appearance_turn": 111,
            "last_appearance_game_day": 5,
            "appearance_count": 3,
        },
        "friend": {
            "character_id": "friend",
            "name": "Марк",
            "card_ref": "friend",
            "origin": "story_created",
            "importance": "recurring",
            "story_function": "Друг POV и источник медицинской линии.",
            "status": "active",
            "last_contact_turn": 98,
            "last_contact_game_day": 4,
            "appearance_count": 7,
        },
    }
    index = cast_registry_runtime._registry_index(registry)
    assert [row["character_id"] for row in index] == ["core", "friend"]
    assert index[0]["last_appearance_turn"] == 111
    assert index[1]["last_contact_game_day"] == 4
