import pytest

from app import narrative_guardrails_runtime as guardrails


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


def test_valid_scene_format_is_accepted():
    guardrails._validate_scene_output(VALID_SCENE)


def test_scene_format_rejects_missing_exact_option_count():
    broken = VALID_SCENE.replace("3. Остаться на месте.\n", "")
    with pytest.raises(RuntimeError, match="SCENE_FORMAT_INVALID"):
        guardrails._validate_scene_output(broken)


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
