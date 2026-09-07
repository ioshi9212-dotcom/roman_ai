from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_single_chunk_operations_are_exposed_for_response_size_hotfix():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    paths = schema["paths"]
    turn = paths["/sessions/{session_id}/turn-packet/{packet_id}/{chunk_index}"]["get"]
    audit = paths["/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}"]["get"]
    assert turn["operationId"] == "getTurnPacketChunk"
    assert audit["operationId"] == "getAuditSnapshotChunk"
    assert "/sessions/{session_id}/turn-packet-batch/{packet_id}" not in paths
    assert "/sessions/{session_id}/audit-snapshot-batch/{audit_id}" not in paths


def test_custom_gpt_hotfix_uses_inline_chunk_zero_then_single_reads_only():
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    runtime_guide = (ROOT / "runtime" / "custom_gpt.md").read_text(encoding="utf-8")

    assert "first_chunk_included=true" in instructions
    assert "НЕ запрашивать chunk 0 снова" in instructions
    assert "getTurnPacketChunk" in instructions
    assert "Не использовать batch" in instructions
    assert "getAuditSnapshotChunk" in instructions
    assert "first_chunk_included=true" in runtime_guide
    assert "Read only remaining packet chunks individually" in runtime_guide
    assert "Do not batch" in runtime_guide
    assert "getAuditSnapshotChunk" in runtime_guide
