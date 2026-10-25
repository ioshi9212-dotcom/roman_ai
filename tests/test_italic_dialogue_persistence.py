"""Dialogue speaker recognition is a persistence/privacy contract, not a style detail."""
from app.scene_speech import iter_scene_speech
from app.private_knowledge_runtime import _speaker_units
from app.session_recovery import _speaker_present_ids
from app.turn_pipeline import _relationship_scene_participants


CARDS = [
    {"character_id": "pov", "name": "Рината", "is_pov": True},
    {"character_id": "dante", "name": "Данте"},
]

CURRENT = """**◈ История** · лето
◷ День 1 · суббота, 02.05.2026 · 10:00
⌖ двор · ☁︎ ясно
▸ разговор
────────────────
◇ **Рината** · джинсы · хвост
▣ При себе: ключи
--------------------------------------------------------
**Данте** *Ты что, серьёзно?* (Смотрит на дверь.)
Рината обернулась.

Что я могу сделать:
1. Выйти.
2. Сесть.
3. Посмотреть.

Что я могу сказать:
1. Да.
2. Нет.
3. Не знаю.

Что я могу подумать:
1. Злюсь.
2. Удивлена.
3. Смешно.

Состояние: раздражена
Отношения:
Ход 1 · цикл 1/15"""

LEGACY = "> **Данте** — Ты что, серьёзно? (Смотрит на дверь.)"


def test_scene_speech_supports_author_preferred_italics_and_legacy_dash():
    current = list(iter_scene_speech(CURRENT))
    old = list(iter_scene_speech(LEGACY))
    assert len(current) == 1
    assert current[0]["speaker"] == old[0]["speaker"] == "Данте"
    assert current[0]["text"] == old[0]["text"] == "Ты что, серьёзно? (Смотрит на дверь.)"


def test_privacy_and_recovery_see_current_italic_speaker():
    units = _speaker_units(CURRENT, CARDS)
    assert [item["character_id"] for item in units] == ["dante"]
    assert "Ты что, серьёзно?" in units[0]["text"]
    assert _speaker_present_ids(CURRENT, cards=CARDS) == ["dante"]


def test_italic_speaker_counts_as_actual_turn_relationship_participant():
    state = {"pov": {"character_id": "pov"}, "current": {"present_characters": ["pov"]}}
    participants = _relationship_scene_participants(
        state, state, {}, cards=CARDS, user_input="", scene_output=CURRENT
    )
    assert "dante" in participants


def test_non_dialogue_bold_subheadings_not_mistaken_for_npc():
    assert list(iter_scene_speech("**Важно** обычное описание\n**Название**")) == []
