from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_oversized_direct_character_reads_are_not_exposed_in_openapi():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    operation_ids = {
        operation.get("operationId")
        for path_item in spec.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict)
    }
    assert "getCharacterBundle" not in operation_ids
    assert "getCharacterMemory" not in operation_ids
    assert "prepareCharacterBundleRead" in operation_ids
    assert "getCharacterBundleChunk" in operation_ids
    assert "getTurnPacketChunkBatch" not in operation_ids
    assert "getAuditSnapshotChunkBatch" not in operation_ids


def test_backend_legacy_character_endpoints_still_exist_for_compatibility():
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'operation_id="getCharacterBundle"' in main
    assert 'operation_id="getCharacterMemory"' in main
    assert 'operation_id="prepareCharacterBundleRead"' in main
    assert 'operation_id="getCharacterBundleChunk"' in main


def test_turn_context_is_scene_scoped_not_all_character_full_copy():
    context = (ROOT / "app" / "turn_context.py").read_text(encoding="utf-8")
    assert 'context["character_cards"] = scene_cards' in context
    assert 'context["character_memory"] = scene_memory' in context
    assert 'context["all_character_cards"]' not in context
    assert 'context["memory_full"]' not in context


def test_gpt_instructions_use_single_safe_chunks_and_on_demand_dossier():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "getTurnPacketChunk" in text
    assert "prepareCharacterBundleRead" in text
    assert "getCharacterBundleChunk" in text
    assert "getCharacterBundle`/`getCharacterMemory" in text
    assert "getTurnPacketChunkBatch" not in text
    assert len(text) <= 8000
