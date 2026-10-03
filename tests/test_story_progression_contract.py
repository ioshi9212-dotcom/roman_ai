from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_uses_normal_and_important_targets_without_minimum_default():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "2000–2400 непробельных символов" in text
    assert "вплоть до ~1500" in text
    assert "Не выбирай 1500 как обычную цель" in text
    assert "2500–3000 непробельных символов" in text
    assert "НЕ означает искусственно удерживать персонажей в одном эпизоде" in text


def test_scene_endings_leave_a_concrete_playable_hook():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "конкретный активный крючок" in text
    assert "можно просто побыть" in text
    assert "Не придумывай новый активный элемент" in text
    assert "не обязан быть клиффхэнгером в драматическом смысле" in text


def test_scene_builder_has_no_meaningful_choice_or_pacing_lock():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "ТОЧКА ОСТАНОВКИ СЦЕНЫ" not in text
    assert "Если такой точки ещё нет — сцена ещё не закончена." not in text
    assert "ROUTINE" not in text
    assert "STANDARD" not in text
    assert "CINEMATIC" not in text
    assert "важную сцену нельзя заканчивать" in text


def test_rules_keep_world_active_without_turn_count_quota():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert "Мир не ждёт POV" in rules
    assert "нет обязательной частоты событий" in rules
    assert "Не регулируй темп счётчиком ходов" in rules
    assert "Не удерживай бытовую, романтическую или любую другую сцену" in rules
    assert "механическую квоту событий" in rules


def test_prompt_facing_rules_do_not_embed_literal_example_payloads():
    paths = [
        ROOT / "runtime" / "rules.md",
        ROOT / "runtime" / "scene_builder.md",
        ROOT / "gpt" / "custom_gpt_instructions.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8").casefold()
        assert "for example:" not in text, path
        assert '"examples"' not in text, path
