"""Regression tests for the scene format users actually see in Roman AI.

Scene Builder owns the artistic choices; the validator checks header/footer only.
"""
import pytest

from app.scene_format import validate_scene_output


OLD = """🎭 Проба · лето
🕒 День 1 · пятница, 02.05.2026, 12:00 · 📍 двор
🌦️ Погода: солнечно
⚙️ Сцена: разговор у машины
✦ Рината
🧥 Одежда, волосы: джинсы, собраны
--------------------------------------------------------
Рината смотрит на дверь.
**Данте** *Да я её починю.* (Тянет ручку.)

Что я могу сделать:
1. Осмотреть.
2. Задержаться.
3. Подойти.

Что я могу сказать:
1. Правда?
2. Зачем?
3. Погоди.

Что я могу подумать:
1. Странно.
2. Любопытно.
3. Смешно.

Состояние: бодра
Отношения:
Данте - доверие 12
Ход 1 · цикл 1/15"""


CURRENT = """**◈ Проба** · лето
◷ День 1 · пятница, 02.05.2026 · 12:00
⌖ двор · ☁︎ солнечно
▸ разговор у машины
────────────────
◇ **Рината** · джинсы · волосы собраны
▣ При себе: ключи
--------------------------------------------------------

Данте подцепил ручку. Рината видела, как перекосило железо.
**Данте** *Да я её починю.* (Дёрнул ещё раз.)

Что я могу сделать:
1. Осмотреть крепления.
2. Перехватить дверь.
3. Отложить ключи.

Что я могу сказать:
1. Ты серьёзно?
2. Убери руки.
3. Теперь объясняй.

Что я могу подумать:
1. Невероятно.
2. Какая наглость.
3. Как это вообще чинить?

Состояние: раздражена
Отношения:
Данте - напряжение 12
Ход 1 · цикл 1/15"""


@pytest.mark.parametrize("scene", [OLD, CURRENT])
def test_both_published_scene_header_formats_are_accepted(scene):
    assert validate_scene_output(scene) == scene


def test_new_format_missing_inventory_is_rejected():
    with pytest.raises(ValueError, match="▣ При себе"):
        validate_scene_output(CURRENT.replace("▣ При себе: ключи\n", ""))


def test_current_format_bad_section_order_is_rejected():
    bad = CURRENT.replace("Что я могу сделать:", "Что я могу ПОДЕЛАТЬ:")
    with pytest.raises(ValueError, match="option_sections"):
        validate_scene_output(bad)


def test_old_header_rejected_if_missing_required_field():
    with pytest.raises(ValueError, match="🌦️ Погода"):
        validate_scene_output(OLD.replace("🌦️ Погода: солнечно\n", ""))
