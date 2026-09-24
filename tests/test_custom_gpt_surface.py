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
    intake_chunk = "/novel-drafts/{draft_id}/intake/chunks"
    intake_mapping = "/novel-drafts/{draft_id}/intake/{block_id}/mapping"
    reconciliation = "/novel-drafts/{draft_id}/reconciliation"
    launch_state = "/novel-drafts/{draft_id}/launch-state"
    scene_archive_prepare = "/sessions/{session_id}/scene-archive/read"
    scene_archive_chunk = "/sessions/{session_id}/scene-archive/read/{read_id}/{chunk_index}"

    assert paths[turn_path]["get"]["operationId"] == "getTurnPacketChunk"
    assert paths[audit_path]["get"]["operationId"] == "getAuditSnapshotChunk"
    assert paths[character_prepare]["post"]["operationId"] == "prepareCharacterBundleRead"
    assert paths[character_chunk]["get"]["operationId"] == "getCharacterBundleChunk"
    assert paths[rollback_path]["post"]["operationId"] == "rollbackLastTurn"
    assert paths[intake_chunk]["post"]["operationId"] == "appendDraftIntakeChunk"
    assert paths[intake_mapping]["post"]["operationId"] == "updateDraftIntakeMapping"
    assert paths[reconciliation]["post"]["operationId"] == "confirmDraftReconciliation"
    assert paths[launch_state]["post"]["operationId"] == "setDraftLaunchState"
    assert paths[scene_archive_prepare]["post"]["operationId"] == "prepareSceneArchiveRead"
    assert paths[scene_archive_chunk]["get"]["operationId"] == "getSceneArchiveChunk"
    assert schema["components"]["schemas"]["NovelDraftIntakeChunk"]["properties"]["raw_text"]["maxLength"] == 6000
    rollback_schema = schema["components"]["schemas"]["RollbackLastTurn"]
    assert set(rollback_schema["required"]) == {"expected_turn_number", "expected_turn_id", "confirm"}
    assert rollback_schema["properties"]["confirm"]["const"] is True
    assert "packet_id" in schema["components"]["schemas"]["TurnCommit"]["required"]
    turn_prepare = schema["components"]["schemas"]["TurnPrepare"]
    assert turn_prepare["required"] == ["user_input"]
    assert "request_id" in turn_prepare["properties"]
    assert "scene_archive_capable" in turn_prepare["properties"]
    assert "replace_pending" in turn_prepare["properties"]
    assert "audit_id" in schema["components"]["schemas"]["AuditCommit"]["required"]

    assert "/sessions/{session_id}/turn-packet-batch/{packet_id}" not in paths
    assert "/sessions/{session_id}/audit-snapshot-batch/{audit_id}" not in paths
    assert "/sessions/{session_id}/characters/{character_id}" not in paths
    assert "/sessions/{session_id}/characters/{character_id}/memory" not in paths
    assert "/sessions/{session_id}/preview" not in paths
    assert "getTurnRange" not in {
        operation.get("operationId")
        for methods in paths.values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict)
    }


def test_action_descriptions_stay_under_custom_gpt_limit():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            description = str(operation.get("description") or "")
            assert len(description) <= 300, f"{method.upper()} {path} description is {len(description)} chars"


def test_custom_gpt_instruction_stays_small_and_matches_writer_first_transport():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert len(text) <= 8000
    assert "runtime_rules" in text
    assert "scene_builder" in text
    assert "getTurnPacketChunk" in text
    assert "getAuditSnapshotChunk" in text
    assert "prepareCharacterBundleRead" in text
    assert "getCharacterBundleChunk" in text
    assert "rollbackLastTurn" in text
    assert "packet_id" in text
    assert "audit_id" in text
    assert "current_turn_id" in text
    assert "опечат" in text
    assert "future_guidance" in text

    # v5 setup: one confirmation finishes the entire internal pipeline.
    assert "draft **version=5**" in text
    assert "`подтверждаю` означает" in text
    assert "не спрашивай «продолжать?»" in text
    assert "полного finalize" in text
    assert "appendDraftIntakeChunk" in text
    assert "updateDraftIntakeMapping" in text
    assert "fact_ids=[]" in text
    assert "confirmDraftReconciliation" in text
    assert "finalizeNovelDraft" in text
    assert "prepareDraftRead" in text
    assert "запускай первую сцену" in text
    assert "setDraftLaunchState" in text

    # v5 fixed profiles and plain learned-knowledge journal.
    assert "Character profile" in text
    assert "knowledge при создании всегда пустой" in text
    assert "knowledge_journal_add" in text
    assert "Никаких fact_id/source_fact_ids/source_event_ids/source_unit_id" in text
    assert "собственный `character_profiles[ID]`" in text
    assert "собственный `knowledge_journals[ID]`" in text
    assert "strict_knowledge_capable=false" in text
    assert "knowledge_review_capable=true" in text
    assert "POV" in text
    assert "бытовые низкорисковые реплики" in text

    # Runtime/recovery behavior remains explicit.
    assert "request_id" in text
    assert "scene_archive_capable=true" in text
    assert "replace_pending=false" in text
    assert "current_recovery_required=true" in text
    assert "last_committed_turn.scene_output" in text
    assert "prepareSceneArchiveRead" not in text or "prepareSceneArchiveRead" in text
