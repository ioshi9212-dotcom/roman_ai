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


def test_custom_gpt_hotfix_forbids_large_batch_reads():
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    runtime_guide = (ROOT / "runtime" / "custom_gpt.md").read_text(encoding="utf-8")

    assert "Прочитать ВСЕ chunks ПО ОДНОМУ через `getTurnPacketChunk`" in instructions
    assert "Не использовать batch Action" in instructions
    assert "getAuditSnapshotChunk" in instructions
    assert "Read every packet chunk individually with `getTurnPacketChunk`" in runtime_guide
    assert "Do not batch Action responses" in runtime_guide
    assert "getAuditSnapshotChunk" in runtime_guide
