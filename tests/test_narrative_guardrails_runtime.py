import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import narrative_guardrails_runtime as guardrails
from app import session_runtime, storage
from app.models import TurnCommit


VALID_SCENE = """🎭 Тест · осень
🕒 День 1 · вторник, 08.09.2026, 12:00 · 📍 база
🌦️ Погода: прохладно
⚙️ Сцена: короткая тестовая сцена
✦ Елена
🧥 Одежда, волосы: куртка, волосы собраны
--------------------------------------------------------

Елена вошла в помещение.

Что я могу сделать:
1. Осмотреться.
2. Подойти ближе.
3. Остаться на месте.

Что я могу сказать:
1. Поздороваться.
2. Задать вопрос.
3. Промолчать.

Что я могу подумать:
1. Оценить обстановку.
2. Вспомнить задачу.
3. Решить, кому доверять.

Состояние: собрана, насторожена
Отношения:
Лиам - доверие 2

Ход 1 · цикл 1/15"""


REVIEWED_EMPTY = {
    "persistence_reviewed": True,
    "chronology": [],
    "knowledge_add": [],
    "experiences_add": [],
    "dialogue_memory_add": [],
    "npc_intent_updates": [],
    "story_thread_updates": [],
}


def _setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_valid_scene_format_is_accepted():
    guardrails._validate_scene_output(VALID_SCENE)


def test_valid_scene_format_accepts_exact_inline_header_from_scene_builder():
    inline = VALID_SCENE.replace(
        "🕒 День 1 · вторник, 08.09.2026, 12:00 · 📍 база\n🌦️ Погода: прохладно",
        "🕒 День 1 · вторник, 08.09.2026, 12:00 ·\n📍 база 🌦️ Погода: прохладно",
    ).replace(
        "✦ Елена\n🧥 Одежда, волосы: куртка, волосы собраны",
        "✦ Елена 🧥 Одежда, волосы: куртка, волосы собраны ◈ Инвентарь: телефон",
    )
    guardrails._validate_scene_output(inline)
    model = TurnCommit(packet_id="packet", user_input="test", scene_output=inline, extracted=REVIEWED_EMPTY)
    assert "📍 база 🌦️ Погода: прохладно" in model.scene_output
    assert "✦ Елена 🧥 Одежда, волосы:" in model.scene_output


def test_scene_format_rejects_missing_exact_option_count():
    broken = VALID_SCENE.replace("3. Остаться на месте.\n", "")
    with pytest.raises(ValueError, match="SCENE_FORMAT_INVALID"):
        guardrails._validate_scene_output(broken)


def test_turn_commit_model_enforces_scene_builder_and_story_review_at_api_boundary():
    broken = VALID_SCENE.replace("Что я могу подумать:", "Мысли:")
    with pytest.raises(ValidationError, match="SCENE_FORMAT_INVALID"):
        TurnCommit(packet_id="packet", user_input="тест", scene_output=broken, extracted=REVIEWED_EMPTY)
    with pytest.raises(ValidationError, match="story_thread_updates"):
        TurnCommit(
            packet_id="packet",
            user_input="тест",
            scene_output=VALID_SCENE,
            extracted={key: value for key, value in REVIEWED_EMPTY.items() if key != "story_thread_updates"},
        )
    model = TurnCommit(packet_id="packet", user_input="тест", scene_output=VALID_SCENE, extracted=REVIEWED_EMPTY)
    assert model.scene_output == VALID_SCENE
    assert model.extracted.story_thread_updates == []


def test_cast_pressure_surfaces_long_absent_important_npc_and_intent():
    context = {
        "cast_index": [
            {"character_id": "pov", "name": "Елена", "is_pov": True, "present": True},
            {"character_id": "liam", "name": "Лиам", "role": "major", "last_seen_turn": 10},
            {"character_id": "june", "name": "Юна", "role": "support", "last_seen_turn": 48},
        ]
    }
    state = {"npc_intents": {"june": [{"intent_id": "ask", "status": "active"}]}}
    pressure = guardrails._cast_pressure(context, state, 50)
    ids = [row["character_id"] for row in pressure]
    assert "liam" in ids
    assert "june" in ids
    assert next(row for row in pressure if row["character_id"] == "june")["active_intent"] is True
    assert all(row["must_reconsider"] is True for row in pressure)


def test_story_pressure_surfaces_overdue_or_high_priority_threads():
    context = {
        "active_threads": {
            "war": {"summary": "Военная операция", "priority": "high", "last_progress_turn": 5},
            "coffee": {"summary": "Выпить кофе", "priority": "low", "last_progress_turn": 18},
        }
    }
    pressure = guardrails._story_pressure(context, 25)
    assert [row["thread_id"] for row in pressure] == ["war"]
    assert pressure[0]["turns_since_progress"] == 20


def test_character_relevance_extracts_existing_card_hooks_without_inventing_history():
    context = {
        "character_cards": [
            {
                "character_id": "elena",
                "name": "Елена",
                "psychology": {"fears": ["замкнутые пространства"], "weakness": "не просит помощи"},
                "appearance": {"hair": "короткие"},
            }
        ]
    }
    relevance = guardrails._character_relevance(context)
    assert relevance
    facts = " ".join(hook["fact"] for hook in relevance[0]["card_hooks"])
    assert "замкнутые пространства" in facts
    assert "не просит помощи" in facts
    assert "короткие" not in facts



def test_character_driven_behavior_is_short_and_character_driven():
    rule = guardrails._character_driven_behavior_rule()
    assert rule["mandatory"] is True
    text = rule["rule"]
    assert "характеру" in text
    assert "целям" in text
    assert "знаниям" in text
    assert "отношениям" in text
    assert "не по тому, как правильно" in text
    assert "Значимая реакция POV" in text
    assert len(text) < 180


def test_scene_momentum_is_short_and_keeps_meaningful_boundary():
    rule = guardrails._scene_momentum_rule({})
    assert rule["mandatory"] is True
    assert rule["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
    text = rule["rule"]
    assert "Рутину" in text
    assert "Короткое действие" in text
    assert "следующий самостоятельный этап" in text
    assert "Важную сцену не проматывай" in text
    assert len(text) < 360


def test_pov_activity_keeps_visible_presence_and_ordinary_dialogue():
    rule = guardrails._pov_activity_rule()
    assert rule["mandatory"] is True
    assert rule["min_post_input_presence_beats"] == 2
    assert rule["ordinary_dialogue_required_when_natural"] is True
    assert rule["multiple_pov_lines_allowed"] is True
    assert rule["silence_requires_character_or_scene_reason"] is True
    assert rule["do_not_replace_speech_with_gesture"] is True
    text = rule["rule"]
    assert "не камера и не мебель" in text
    assert "минимум дважды" in text
    assert "сам user_input не засчитывай" in text
    assert "POV отвечает словами" in text
    assert "не заменяй естественный ответ" in text


def test_npc_intent_drive_treats_evasion_as_unresolved():
    context = {
        "npc_active_intents": {
            "ren": [{
                "intent_id": "get_answer",
                "summary": "Добиться ответа, где POV была ночью",
                "priority": 75,
                "eligible_now": True,
            }]
        }
    }
    drive = guardrails._npc_intent_drive_rule(context)
    assert drive["mandatory"] is True
    assert drive["active_intent_count"] == 1
    assert "до resolve/abandon" in drive["rule"]
    assert "Увиливание" in drive["rule"]
    assert "не закрывают" in drive["rule"]


def test_scene_momentum_uses_meaningful_choice_not_literal_last_action_as_boundary():
    context = {
        "player_input_map": {
            "ordered_segments": [{"kind": "stage_direction", "text": "добраться до смены и работать как обычно"}],
            "stage_directions": ["добраться до смены и работать как обычно"],
            "spoken_segments": [],
        }
    }
    rule = guardrails._scene_momentum_rule(context)
    assert rule["mandatory"] is True
    scope = rule["player_input_scope"]
    assert scope["boundary"] == "next_meaningful_pov_choice"
    assert scope["last_segment_kind"] == "stage_direction"
    assert "добраться до смены" in scope["last_segment_text"]
    assert "Рутину веди до конца" in rule["rule"]


def test_scene_momentum_does_not_turn_short_meaningful_action_into_whole_new_phase():
    context = {
        "player_input_map": {
            "ordered_segments": [{"kind": "stage_direction", "text": "лечь, обнимая его"}],
            "stage_directions": ["лечь, обнимая его"],
            "spoken_segments": [],
        }
    }
    rule = guardrails._scene_momentum_rule(context)
    assert rule["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
    assert "Короткое действие доводи до результата и реакции" in rule["rule"]
    assert "не проживай за POV следующий самостоятельный этап" in rule["rule"]


def test_prepare_turn_packet_contains_concise_narrative_guardrails():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        novel = {
            "novel_id": "guardrail_packet",
            "title": "Guardrail Packet",
            "novel": {"pov_character": "elena"},
            "characters": [
                {"character_id": "elena", "name": "Елена", "is_pov": True},
                {"character_id": "liam", "name": "Лиам", "role": "major"},
            ],
            "lore": {},
            "starting_state": {
                "current": {"location": "база", "present_characters": ["elena"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        packet = session_runtime.prepare_turn_packet(sid, "(работать до конца смены)")
        parts = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            parts.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(parts))

        signals = context["narrative_guardrails"]
        assert signals["version"] == 14
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["pov_activity"]["min_post_input_presence_beats"] == 2
        assert signals["pov_activity"]["ordinary_dialogue_required_when_natural"] is True
        assert signals["character_driven_behavior"]["mandatory"] is True
        assert signals["npc_intent_drive"]["mandatory"] is True
        assert signals["scene_momentum"]["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
        assert signals["story_drive"]["mandatory"] is True
        assert isinstance(signals["cast_pressure"], list)
        assert len(json.dumps(signals, ensure_ascii=False)) < 9000

