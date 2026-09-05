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


def test_custom_gpt_hotfix_forbids_large_batch_reads():
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    runtime_guide = (ROOT / "runtime" / "custom_gpt.md").read_text(encoding="utf-8")

    assert "НЕ использовать batch-чтение turn packet" in instructions
    assert "getTurnPacketChunk(session_id, packet_id, chunk_index)" in instructions
    assert "НЕ использовать batch" in instructions
    assert "getAuditSnapshotChunk(session_id, audit_id, chunk_index)" in instructions

    assert "do not use `getTurnPacketChunkBatch`" in runtime_guide
    assert "getTurnPacketChunk`" in runtime_guide
    assert "Do not use the audit batch endpoint" in runtime_guide
