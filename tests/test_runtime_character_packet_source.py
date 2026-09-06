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
    turn_context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    session_runtime = (ROOT / "app" / "session_runtime.py").read_text(encoding="utf-8")
    transport_scope = (ROOT / "app" / "transport_scope_runtime.py").read_text(encoding="utf-8")

    assert 'context["character_cards"] = scene_cards' in turn_context
    assert 'context["character_memory"] = scene_memory' in turn_context
    assert 'context["character_registry"] = registry' in session_runtime
    assert '"all_character_cards"' in transport_scope
    assert '"memory_full"' in transport_scope
    assert 'result.pop(key, None)' in transport_scope
