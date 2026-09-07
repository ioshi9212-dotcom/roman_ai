from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_writer_contract_forbids_direction_notes_from_becoming_shared_history():
    text = (ROOT / "runtime" / "writer_contract.md").read_text(encoding="utf-8")
    assert "AUTHOR GUIDANCE IS NOT EXPERIENCED HISTORY" in text
    assert "story_direction" in text
    assert "NOT evidence that a scene" in text
    assert "this time" in text
    assert "he had already told her" in text
    assert "character card trait" in text
    assert "What exact earlier committed event" in text


def test_custom_gpt_checks_provenance_before_callbacks_and_comparisons():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "РЕЖИССУРА ≠ ПРОШЛОЕ" in text
    assert "Рен уже рассказывал ей" in text
    assert "снова" in text
    assert "в этот раз" in text
    assert "provenance любого упоминания прошлого" in text
