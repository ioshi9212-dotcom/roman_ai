from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_floor_is_2000_chars():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "2000–3000 символов" in text
    assert "1500–3000 символов" not in text
    assert "ближе к 1500" not in text


def test_scene_endings_avoid_authorial_curtain_and_forecast():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Последний абзац не должен быть авторским занавесом" in text
    assert "первый барьер оказался позади" in text
    assert "дальше оставалось" in text
    assert "не объясняй читателю сверху" in text


def test_scene_builder_requires_episode_progression_without_freezing_pov():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "один микромомент не должен занимать десятки ходов" in text
    assert "4 из последних 6 ходов" in text
    assert "Не ускоряй через значимое решение за POV" in text


def test_source_genres_cast_and_pov_biography_are_active_material():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "не декоративная справка" in text
    assert "not_encountered" in text
    assert "Все явно заданные жанры и крупные линии остаются активными одновременно" in text
    assert "Собственная анкета POV — не склад реквизита" in text
    assert "амнезию" in text
