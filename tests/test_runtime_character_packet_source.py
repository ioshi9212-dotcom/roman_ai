from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_uses_chunked_offscreen_character_dossier_action():
    continuity = (ROOT / "runtime" / "continuity_contract.md").read_text(encoding="utf-8")
    runtime_access = (ROOT / "app" / "runtime_access.py").read_text(encoding="utf-8")
    assert "prepareCharacterBundleRead" in continuity
    assert "getCharacterBundleChunk" in continuity
    assert "oversized direct character bundle/memory Action" in continuity
    assert "prepareCharacterBundleRead" in runtime_access
    assert "getCharacterBundleChunk" in runtime_access


def test_turn_context_contains_only_scene_scoped_full_character_sources():
    context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    assert 'context["character_cards"] = scene_cards' in context
    assert 'context["character_memory"] = scene_memory' in context
    assert 'context["character_registry"]' in context
    assert 'context["all_character_cards"]' not in context
    assert 'context["memory_full"]' not in context
