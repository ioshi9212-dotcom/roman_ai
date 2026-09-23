from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_simple_runtime_forbids_direction_notes_from_becoming_shared_history():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    combined = rules + "\n" + builder
    assert "future_guidance" in combined
    assert "не уже произошедшие события" in combined
    assert "dialogue_frame" in combined
    assert "до реплики" in combined
    assert "реальным репликам" in combined


def test_custom_gpt_keeps_history_memory_and_future_guidance_separate():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "ДАННЫЕ НЕ СМЕШИВАТЬ" in text
    assert "future_guidance" in text
    assert "не фактический источник реплики" in text
    assert "knowledge_path" in text
    assert "claims_reviewed=true" in text
    assert "turn_knowledge" in text
