from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_floor_is_2000_chars():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "2000–3000 символов" in text
    assert "1500–3000 символов" not in text
    assert "ближе к 1500" not in text


def test_scene_endings_avoid_authorial_curtain_and_forecast():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Финал сцены должен оставаться внутри происходящего" in text
    assert "авторским резюме" in text
    assert "прогнозом" in text
    assert "литературный абзац" in text


def test_scene_builder_requires_episode_progression_without_freezing_pov():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Один микромомент не должен занимать десятки ходов" in text
    assert "4 из последних 6 ходов" in text
    assert "Не ускоряй сюжет через значимое решение за POV" in text


def test_source_genres_cast_and_pov_biography_are_active_material():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "обязательны для ведения истории" in text
    assert "Важные активные персонажи не должны исчезать" in text
    assert "Все заявленные жанры и крупные линии остаются активными" in text
    assert "Собственная анкета POV должна естественно проявляться" in text
    assert "скрытые истины" in text


def test_offscreen_npcs_cannot_know_current_scene_without_a_source():
    scene = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    turn_context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    assert "отсутствующий NPC не реагирует на текущую сцену без установленного источника знания" in scene
    assert "удалённое сообщение или звонок не содержит фактов, которых отправитель не мог получить" in scene
    assert "MANDATORY KNOWLEDGE FIREWALL" in turn_context
    assert "Before an offscreen NPC sends a message, calls or reacts to a current event" in turn_context
    assert "author knows it" in turn_context


def test_prompt_facing_rules_do_not_embed_examples():
    paths = [
        *sorted((ROOT / "runtime").glob("*.md")),
        ROOT / "gpt" / "custom_gpt_instructions.md",
        ROOT / "app" / "turn_context.py",
        ROOT / "app" / "scene_presence_runtime.py",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8").casefold()
        assert "пример" not in text, path
        assert "for example" not in text, path
        assert '"examples"' not in text, path
