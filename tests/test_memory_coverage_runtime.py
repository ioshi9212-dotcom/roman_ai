"""Evidence guard must notice omitted durable dialogue, not hallucinate memories."""
import pytest
from fastapi import HTTPException

from app.memory_coverage_runtime import durable_speech_cues, validate_durable_dialogue_coverage


def scene(line="**Данте** *Обещаю, завтра принесу ключи.* (Отвернулся.)"):
    return line + """

Что я могу сделать:
1. Выйти.
2. Сесть.
3. Посмотреть.
Что я могу сказать:
1. Обещаю всё исправить.
2. Нет.
3. Ладно.
Что я могу подумать:
1. Странно.
2. Злюсь.
3. Проверю.
"""


def extracted(**updates):
    payload = {"knowledge_add": [], "experiences_add": [],
               "dialogue_memory_add": [], "knowledge_journal_add": [], "chronology": []}
    payload.update(updates)
    return payload


def test_explicit_spoken_promise_cannot_disappear_into_empty_commit():
    with pytest.raises(HTTPException) as exc:
        validate_durable_dialogue_coverage({
            "scene_output": scene(), "extracted": extracted()
        })
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "DURABLE_DIALOGUE_NOT_PERSISTED"
    assert "Данте" == exc.value.detail["cues"][0]["speaker"]


def test_promise_is_not_assigned_to_unwitnessing_npc_by_guard():
    evidence = [{"character_id": "dante", "summary": "Данте пообещал принести ключи POV."}]
    assert validate_durable_dialogue_coverage({
        "scene_output": scene(), "extracted": extracted(experiences_add=evidence)
    }) is None


def test_hypothetical_choices_do_not_create_memory_events():
    only_choices = "Рина посмотрела на пол.\n" + scene("**Данте** *Ты кто вообще?*")
    assert durable_speech_cues(only_choices) == []
    assert validate_durable_dialogue_coverage({
        "scene_output": only_choices, "extracted": extracted()
    }) is None


def test_old_dash_dialogue_protected_too():
    with pytest.raises(HTTPException):
        validate_durable_dialogue_coverage({
            "scene_output": scene("> **Данте** — Даю слово, я вернусь."),
            "extracted": extracted()
        })


def test_plain_emotions_without_explicit_oath_do_not_trigger():
    assert validate_durable_dialogue_coverage({
        "scene_output": scene("**Данте** *Свали, блять.* (Скривился.)"),
        "extracted": extracted()
    }) is None
