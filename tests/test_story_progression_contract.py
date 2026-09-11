from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_floor_is_2000_chars():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "2000–3000 символов" in text
    assert "1500–3000 символов" not in text


def test_scene_endings_avoid_authorial_curtain_and_forecast():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "естественный импульс к следующему ходу" in text
    assert "Последние абзацы сцены должны не закрывать движение" in text
    assert "Запрещены пустые финалы" in text
    assert "сцена завершена неправильно" in text


def test_scene_builder_requires_progression_without_freezing_pov():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Нельзя заканчивать сцену в нейтральной точке" in text
    assert "ничего не требует реакции" in text
    assert "передавать его следующему ходу" in text


def test_source_lines_cast_and_pov_remain_active_without_prompt_bloat():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Быт не должен замораживать мир" in rules
    assert "активные threads/intents" in rules
    assert "Вне сцены важные НПС также живут" in builder
    assert "POV всегда присутствует как живой персонаж" in builder
    assert "реальные разные продолжения сцены" in builder


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
