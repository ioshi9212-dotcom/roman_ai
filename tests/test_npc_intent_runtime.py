import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage
from app.character_access import get_character_bundle


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "npc-intents",
        "title": "NPC Intents",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "ren", "name": "Ren", "traits": ["stubborn", "suspicious"]},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "home", "scene": "alone", "present_characters": ["pov"], "game_day": 1},
        },
    }


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    pieces = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if pieces else 0
    for index in range(start, manifest["chunk_count"]):
        pieces.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(pieces))


def base_extracted(**overrides):
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
    }
    value.update(overrides)
    return value


def test_unresolved_npc_intent_persists_and_resurfaces_without_player_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        read_packet(sid, "(убрать телефон)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(убрать телефон)",
                "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": base_extracted(
                    npc_intent_updates=[{
                        "character_id": "ren",
                        "intent_id": "check_account_origin",
                        "kind": "investigation",
                        "summary": "Выяснить, кто стоит за странным аккаунтом",
                        "priority": "high",
                        "planned_action": "Проверить происхождение аккаунта и потом вернуться к POV с результатом",
                        "next_eligible_game_day": 3,
                    }]
                ),
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(root / "state.json", {})
        saved = state["npc_intents"]["ren"][0]
        assert saved["intent_id"] == "check_account_origin"
        assert saved["status"] == "active"

        state["current"]["game_day"] = 5
        state["current"]["present_characters"] = ["pov", "ren"]
        state.setdefault("characters", {}).setdefault("ren", {})["present"] = True
        storage._write_json(root / "state.json", state)

        _, context = read_packet(sid, "(посмотреть на Рена)")
        intent = context["npc_active_intents"]["ren"][0]
        assert intent["intent_id"] == "check_account_origin"
        assert intent["eligible_now"] is True
        assert "npc_intent_instruction" not in context
        assert "Смена темы intent не закрывает." in context["runtime_rules"]
        assert "narrative_guardrails" not in context
        assert "working_context_contract" not in context
        assert "Intent остаётся активным" in context["runtime_rules"]

        bundle = get_character_bundle(sid, "ren")
        assert bundle["active_intents"][0]["intent_id"] == "check_account_origin"


def test_offscreen_active_intent_is_visible_before_character_is_pulled_into_scene():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        read_packet(sid, "(заняться своими делами)")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "(заняться своими делами)",
                "scene_output": "POV остаётся дома.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": base_extracted(
                    npc_intent_updates=[{
                        "character_id": "ren",
                        "intent_id": "come_back_to_talk",
                        "summary": "Вернуться к POV и закончить разговор",
                        "priority": "high",
                        "planned_action": "Подойти к POV, когда будет возможность",
                        "next_eligible_game_day": 1,
                    }],
                    state_patch={"characters": {
                        "ren": {"present": False, "location": "home", "activity": "в соседней комнате"}
                    }},
                ),
            },
        )

        _, context = read_packet(sid, "(налить чай)")
        assert "ren" not in context["relevant_character_ids"]
        assert "ren" not in context["npc_active_intents"]
        assert "offscreen_intent_candidates" not in context
        assert context["scene_state"]["characters"]["ren"]["location"] == "home"
        assert context["scene_state"]["characters"]["ren"]["activity"] == "в соседней комнате"
        assert "working_context_contract" not in context
        registry_row = next(
            row for row in context["cast_registry"]["characters"]
            if row["character_id"] == "ren"
        )
        assert registry_row["active_intents"]
        assert "Отсутствие active intent или thread НЕ означает" in context["runtime_rules"]

        # The owner still gets the full private intent once their dossier is loaded.
        bundle = get_character_bundle(sid, "ren")
        assert bundle["active_intents"][0]["intent_id"] == "come_back_to_talk"
        assert "Подойти к POV" in bundle["active_intents"][0]["planned_action"]


def test_intent_source_fact_may_be_added_to_same_character_in_same_commit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "(Рен замечает деталь)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(Рен замечает деталь)",
                "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": base_extracted(
                    knowledge_add=[{"character_id": "ren", "fact_id": "account_created_recently", "content": "Аккаунт создан недавно"}],
                    npc_intent_updates=[{
                        "character_id": "ren", "intent_id": "trace_account", "kind": "investigation",
                        "summary": "Проверить происхождение аккаунта", "source_fact_ids": ["account_created_recently"],
                    }],
                ),
            },
        )
        assert result["ok"] is True
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["npc_intents"]["ren"][0]["source_fact_ids"] == ["account_created_recently"]


def test_intent_cannot_launder_author_only_fact_into_future_npc_behavior():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "(убрать телефон)")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "(убрать телефон)",
                    "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                    "extracted": base_extracted(
                        npc_intent_updates=[{
                            "character_id": "ren", "intent_id": "impossible_followup",
                            "summary": "Действовать на основании неизвестной ему тайны",
                            "source_fact_ids": ["author_only_secret"],
                        }]
                    ),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "NPC_INTENT_SOURCE_FACT_UNKNOWN"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "ren" not in state.get("npc_intents", {})


def test_intent_cannot_copy_pov_only_time_and_place_without_source_fact_ids():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        storage._memory_bucket(memory, "pov")["knowledge_journal"].append({
            "turn": 21,
            "text": "POV узнала от другого человека: выступление будет у стойки после семи.",
        })
        storage._memory_bucket(memory, "ren")["knowledge_journal"].append({
            "turn": 20,
            "text": "Ren знает только, что POV будет играть вечером.",
        })
        storage._write_json(root / "memory.json", memory)

        read_packet(sid, "(продолжить вечер)")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "(продолжить вечер)",
                    "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                    "extracted": base_extracted(
                        npc_intent_updates=[{
                            "character_id": "ren",
                            "intent_id": "come_to_show",
                            "summary": "Прийти к стойке после семи послушать POV",
                            "planned_action": "Быть у стойки после семи",
                        }]
                    ),
                },
            )

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "NPC_INTENT_PERSONAL_KNOWLEDGE_LEAK"
        assert exc.value.detail["character_id"] == "ren"
        state = storage._read_json(root / "state.json", {})
        assert "ren" not in state.get("npc_intents", {})


def test_intent_may_keep_unanswered_detail_as_question_instead_of_inventing_answer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        storage._memory_bucket(memory, "pov")["knowledge_journal"].append({
            "turn": 21,
            "text": "POV узнала от другого человека: выступление будет у стойки после семи.",
        })
        storage._memory_bucket(memory, "ren")["knowledge_journal"].append({
            "turn": 20,
            "text": "Ren знает только, что POV будет играть вечером.",
        })
        storage._write_json(root / "memory.json", memory)

        read_packet(sid, "(продолжить вечер)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(продолжить вечер)",
                "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": base_extracted(
                    npc_intent_updates=[{
                        "character_id": "ren",
                        "intent_id": "find_show_details",
                        "summary": "Узнать, где и во сколько POV будет играть вечером",
                        "planned_action": "Спросить POV о месте и времени",
                    }]
                ),
            },
        )

        assert result["ok"] is True
        state = storage._read_json(root / "state.json", {})
        saved = state["npc_intents"]["ren"][0]
        assert saved["summary"] == "Узнать, где и во сколько POV будет играть вечером"


def test_intent_can_be_marked_pursued_and_resolved():
    from app.npc_intent import apply_updates, active_intents_for

    state = {
        "current": {"game_day": 4},
        "npc_intents": {"ren": [{
            "intent_id": "ask_again", "character_id": "ren",
            "summary": "Вернуться к незакрытому вопросу", "status": "active", "priority": 70,
            "created_turn": 10, "created_game_day": 1,
        }]},
    }
    pursued = apply_updates(state, [{"character_id": "ren", "intent_id": "ask_again", "pursued_now": True}], current_turn=20)
    item = pursued["npc_intents"]["ren"][0]
    assert item["last_pursued_turn"] == 20
    assert item["last_pursued_game_day"] == 4
    assert item["attempt_count"] == 1
    assert item["last_outcome"] == "attempted_without_resolution"

    resolved = apply_updates(
        pursued,
        [{"character_id": "ren", "intent_id": "ask_again", "operation": "resolve", "resolution": "получил ответ"}],
        current_turn=21,
    )
    assert resolved["npc_intents"]["ren"][0]["status"] == "resolved"
    assert active_intents_for(resolved, ["ren"], current_turn=30) == {}


def test_terminal_operation_without_concrete_resolution_keeps_intent_active():
    from app.npc_intent import apply_updates, active_intents_for

    state = {
        "current": {"game_day": 2},
        "npc_intents": {"ren": [{
            "intent_id": "need_answer",
            "character_id": "ren",
            "summary": "Добиться ответа, где POV была ночью",
            "status": "active",
            "priority": 80,
            "created_turn": 3,
            "created_game_day": 1,
        }]},
    }
    result = apply_updates(
        state,
        [{
            "character_id": "ren",
            "intent_id": "need_answer",
            "operation": "resolve",
            "pursued_now": True,
            "last_outcome": "POV отшутилась и сменила тему",
        }],
        current_turn=8,
    )
    item = result["npc_intents"]["ren"][0]
    assert item["status"] == "active"
    assert item["attempt_count"] == 1
    assert item["last_outcome"] == "POV отшутилась и сменила тему"
    active = active_intents_for(result, ["ren"], current_turn=9)
    assert active["ren"][0]["intent_id"] == "need_answer"


def test_future_offscreen_intent_is_not_exposed_as_candidate_before_eligible_day():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        read_packet(sid, "(заняться своими делами)")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "(заняться своими делами)",
                "scene_output": "POV остаётся дома.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": base_extracted(
                    npc_intent_updates=[{
                        "character_id": "ren",
                        "intent_id": "future_visit",
                        "summary": "Вернуться к POV позже",
                        "priority": "high",
                        "planned_action": "Прийти к POV, когда наступит срок",
                        "next_eligible_game_day": 5,
                    }]
                ),
            },
        )

        state = storage._read_json(root / "state.json", {})
        state["current"]["game_day"] = 2
        storage._write_json(root / "state.json", state)

        _, context = read_packet(sid, "(налить чай)")
        candidates = context["offscreen_intent_candidates"]["characters"]
        assert not any(row["character_id"] == "ren" for row in candidates)

        state = storage._read_json(root / "state.json", {})
        state["current"]["game_day"] = 5
        storage._write_json(root / "state.json", state)
        (root / "turn_packet.json").unlink(missing_ok=True)

        _, context = read_packet(sid, "(налить чай)")
        candidates = context["offscreen_intent_candidates"]["characters"]
        assert any(row["character_id"] == "ren" for row in candidates)
