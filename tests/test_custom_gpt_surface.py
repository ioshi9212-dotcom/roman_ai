from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_schema_exposes_only_fixed_batch_context_reads():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    paths = schema["paths"]

    turn_batch = paths["/sessions/{session_id}/turn-packet-batch/{packet_id}"]["get"]
    audit_batch = paths["/sessions/{session_id}/audit-snapshot-batch/{audit_id}"]["get"]

    assert turn_batch["operationId"] == "getTurnPacketChunkBatch"
    assert audit_batch["operationId"] == "getAuditSnapshotChunkBatch"
    assert {item["name"] for item in turn_batch["parameters"]} == {"session_id", "packet_id", "start_index"}
    assert {item["name"] for item in audit_batch["parameters"]} == {"session_id", "audit_id", "start_index"}

    assert "/sessions/{session_id}/turn-packet/{packet_id}/{chunk_index}" not in paths
    assert "/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}" not in paths


def test_action_descriptions_stay_under_custom_gpt_limit():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            description = str(operation.get("description") or "")
            assert len(description) <= 300, f"{method.upper()} {path} description is {len(description)} chars"


def test_custom_gpt_instruction_stays_under_8000_characters():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert len(text) <= 8000
    assert "scene_builder" in text
    assert "runtime rules" in text
    assert "getCharacterBundle" in text
    assert "next_start_index" in text
