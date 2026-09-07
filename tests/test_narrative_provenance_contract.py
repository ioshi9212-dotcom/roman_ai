from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_simple_runtime_forbids_direction_notes_from_becoming_shared_history():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    combined = rules + "\n" + builder
    assert "future_guidance" in combined
    assert "не доказывают" in combined or "не означает" in combined
    assert "в этот раз" in combined
    assert "он уже говорил" in combined
    assert "Рен" in combined
    assert "искра" in combined


def test_custom_gpt_keeps_history_memory_and_future_guidance_separate():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "ДАННЫЕ НЕ СМЕШИВАТЬ" in text
    assert "future_guidance" in text
    assert "снова" in text
    assert "в этот раз" in text
    assert "он уже говорил" in text
    assert "конкретный источник" in text
