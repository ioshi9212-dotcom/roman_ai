from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_schema_exposes_response_safe_single_context_character_and_rollback_actions():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    paths = schema["paths"]

    turn_path = "/sessions/{session_id}/turn-packet/{packet_id}/{chunk_index}"
    audit_path = "/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}"
    character_prepare = "/sessions/{session_id}/characters/{character_id}/read"
    character_chunk = "/sessions/{session_id}/characters/{character_id}/read/{read_id}/{chunk_index}"
    rollback_path = "/sessions/{session_id}/rollback-last-turn"

    assert paths[turn_path]["get"]["operationId"] == "getTurnPacketChunk"
    assert paths[audit_path]["get"]["operationId"] == "getAuditSnapshotChunk"
    assert paths[character_prepare]["post"]["operationId"] == "prepareCharacterBundleRead"
    assert paths[character_chunk]["get"]["operationId"] == "getCharacterBundleChunk"
    assert paths[rollback_path]["post"]["operationId"] == "rollbackLastTurn"
    rollback_schema = schema["components"]["schemas"]["RollbackLastTurn"]
    assert set(rollback_schema["required"]) == {"expected_turn_number", "confirm"}
    assert rollback_schema["properties"]["confirm"]["const"] is True

    assert "/sessions/{session_id}/turn-packet-batch/{packet_id}" not in paths
    assert "/sessions/{session_id}/audit-snapshot-batch/{audit_id}" not in paths
    assert "/sessions/{session_id}/characters/{character_id}" not in paths
    assert "/sessions/{session_id}/characters/{character_id}/memory" not in paths


def test_action_descriptions_stay_under_custom_gpt_limit():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            description = str(operation.get("description") or "")
            assert len(description) <= 300, f"{method.upper()} {path} description is {len(description)} chars"


def test_custom_gpt_instruction_stays_under_8000_characters_and_matches_transport():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert len(text) <= 8000
    assert "scene_builder" in text
    assert "runtime rules" in text
    assert "getTurnPacketChunk" in text
    assert "getAuditSnapshotChunk" in text
    assert "prepareCharacterBundleRead" in text
    assert "getCharacterBundleChunk" in text
    assert "rollbackLastTurn" in text
    assert "reused_pending_packet" in text
    assert "орфограф" in text
