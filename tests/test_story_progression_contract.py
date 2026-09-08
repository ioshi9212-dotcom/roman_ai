from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_scene_length_floor_is_2000_chars():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "2000–3000 символов" in text
    assert "1500–3000 символов" not in text


def test_scene_endings_avoid_authorial_curtain_and_forecast():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Финал сцены должен оставаться внутри происходящего" in text
    assert "Не добавляй авторское резюме" in text
    assert "прогноз" in text
    assert "естественный следующий импульс" in text


def test_scene_builder_requires_progression_without_freezing_pov():
    text = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Не держи один микромомент десятки ходов" in text
    assert "не требует нового важного решения POV" in text
    assert "доведи его до следующего содержательного момента" in text


def test_source_lines_cast_and_pov_remain_active_without_prompt_bloat():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Быт не должен замораживать мир" in rules
    assert "активные threads/intents" in rules
    assert "мир продолжает двигаться" in builder
    assert "POV — полноценный участник" in builder
    assert "future_guidance" in builder


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
