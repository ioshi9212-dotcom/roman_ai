"""A living conversation must stay in long-form continuity, not be replaced by menu labels."""
from app.writer_first_runtime import _compact_continuity_turn, _scene_body_excerpt


def test_excerpt_is_real_dialogue_not_options_or_relationship_footer():
    scene = """**◈ История** · лето
◷ День 1 · суббота, 02.05.2026 · 10:00
⌖ мастерская · ☁︎ ясно
▸ незавершённый спор
────────────────
◇ **Рината** · джинсы · хвост
▣ При себе: ключи
--------------------------------------------------------
Данте усмехнулся, прислонился к двери.
**Данте** *Ты серьёзно собираешься меня выгнать?* (Смотрит на Ринату.)

Что я могу сделать:
1. Подойти.
2. Уйти.
3. Сесть.

Что я могу сказать:
1. Нет.
2. Да.
3. Может быть.

Что я могу подумать:
1. Думаю.
2. Злюсь.
3. Не знаю.

Состояние: раздражена
Отношения:
Данте - злость 40/+1
Ход 3 · цикл 3/15"""
    excerpt = _scene_body_excerpt(scene)
    assert "Ты серьёзно собираешься меня выгнать?" in excerpt
    assert "Что я могу сделать" not in excerpt
    assert "Отношения:" not in excerpt
    assert "При себе:" not in excerpt
    compact = _compact_continuity_turn({"turn_number": 3, "scene_output": scene,
        "extracted": {"chronology": [{"event": "Данте спорил с Ринатой."}]}})
    assert compact["scene_tail"] == excerpt
    assert compact["chronology"]


def test_no_scene_header_falls_back_to_plain_prose():
    assert _scene_body_excerpt("Лина сказала: «Верни часы».") == "Лина сказала: «Верни часы»."
