from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_reuses_full_character_data_already_in_packet():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "all_character_cards" in text
    assert "memory_full.characters" in text
    assert "НЕ вызывать `getCharacterBundle` повторно" in text
    assert "ResponseTooLargeError" in text
    assert len(text) <= 8000
