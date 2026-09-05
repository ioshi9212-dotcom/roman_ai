from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_does_not_require_direct_character_bundle_action():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    continuity = (ROOT / "runtime" / "continuity_contract.md").read_text(encoding="utf-8")
    assert "getCharacterBundle" not in rules
    assert "getCharacterBundle" not in continuity
    assert "all_character_cards" in rules
    assert "memory_full.characters" in rules
    assert "all_character_cards" in continuity
    assert "memory_full.characters" in continuity


def test_turn_context_still_contains_full_character_sources():
    context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    assert '"all_character_cards"' in context
    assert '"memory_full"' in context
