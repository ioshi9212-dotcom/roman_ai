from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_uses_chunked_offscreen_character_dossier_action():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    character_reader = (ROOT / "app" / "character_chunk_read.py").read_text(encoding="utf-8")
    assert "prepareCharacterBundleRead" in rules
    assert "prepareCharacterBundleRead" in instructions
    assert "getCharacterBundleChunk" in instructions
    assert "prepare_character_bundle_read" in character_reader
    assert "get_character_bundle_chunk" in character_reader
    assert "getCharacterBundle`/`getCharacterMemory" in instructions


def test_turn_context_contains_only_scene_scoped_full_character_sources():
    turn_context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    session_runtime = (ROOT / "app" / "session_runtime.py").read_text(encoding="utf-8")
    transport_scope = (ROOT / "app" / "transport_scope_runtime.py").read_text(encoding="utf-8")

    assert 'context["character_cards"] = scene_cards' in turn_context
    assert 'context["character_memory"] = scene_memory' in turn_context
    assert 'context["character_registry"] = registry' in session_runtime
    assert '"all_character_cards"' in transport_scope
    assert '"memory_full"' in transport_scope
    assert 'result.pop(key, None)' in transport_scope
