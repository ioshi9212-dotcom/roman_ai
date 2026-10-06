from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_uses_single_non_whitespace_range():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Обычно сцена может быть около 1800–3000 непробельных символов, но это не лимит и не цель." in text
    assert "Объём основной сцены: 1800–3000 непробельных символов." not in text
    assert "Не добивай объём водой и не обрывай происходящее ради размера." in text

def test_scene_endings_leave_a_concrete_playable_hook():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Продолжай естественные реакции, реплики и уже выбранное действие до следующего момента, где решение игрока действительно может заметно изменить происходящее." in text
    assert "Не останавливайся перед обычным ответом POV, короткой реакцией или очевидным продолжением уже выбранного действия." in text
    assert "Тихая сцена допустима." in text

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
