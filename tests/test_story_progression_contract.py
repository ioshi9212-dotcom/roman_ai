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


def test_new_runtime_rules_do_not_embed_phrase_examples():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Пример:" not in text
    assert "Например" not in text
    assert "например" not in text
