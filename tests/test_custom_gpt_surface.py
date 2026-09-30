from pathlib import Path

import yaml

from app.main import app


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ACTIONS = {
    "rollbackLastTurn",
    "prepareSceneArchiveRead",
    "getSceneArchiveChunk",
    "createNovelDraft",
    "saveNovelDraftSection",
    "appendDraftIntakeChunk",
    "updateDraftIntakeMapping",
    "confirmDraftReconciliation",
    "setDraftLaunchState",
    "finalizeNovelDraft",
    "prepareDraftRead",
    "createSessionFromDraft",
    "getNovelReadChunk",
    "resumeSession",
    "recoverSessionCurrent",
    "prepareTurn",
    "getTurnPacketChunk",
    "prepareCharacterBundleRead",
    "getCharacterBundleChunk",
    "commitTurn",
    "prepareContinuationCompaction",
    "prepareContinuationBlockRead",
    "getContinuationCompactionChunk",
    "commitContinuationBlock",
    "prepareContinuationFinalRead",
    "commitContinuationFinal",
    "createContinuationSession",
    "saveDraftToLibrary",
    "listNovels",
    "createSession",
}


def operation_ids(schema):
    return {
        operation.get("operationId")
        for methods in schema.get("paths", {}).values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"} and isinstance(operation, dict)
    }


def test_static_custom_gpt_schema_has_exact_current_30_actions():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert operation_ids(schema) == EXPECTED_ACTIONS
    assert len(EXPECTED_ACTIONS) == 30

    turn_prepare = schema["components"]["schemas"]["TurnPrepare"]
    assert turn_prepare["required"] == ["user_input"]
    assert set(turn_prepare["properties"]) == {
        "user_input", "request_id", "opening_scene", "scene_archive_capable", "replace_pending"
    }

    turn_commit = schema["components"]["schemas"]["TurnCommit"]
    assert set(turn_commit["required"]) == {"packet_id", "user_input", "scene_output", "extracted"}
    dumped = str(turn_commit)
    assert "knowledge_reviewed" not in dumped
    assert "runtime_contract_version" not in dumped
    assert "relationship_reviewed" not in dumped
    extracted = schema["components"]["schemas"]["TurnExtracted"]
    assert "scene_builder_reviewed" in extracted["properties"]
    assert "persistence_reviewed" in extracted["properties"]
    assert "knowledge_reviewed" in extracted["properties"]

    relationship = schema["components"]["schemas"]["RelationshipUpdate"]
    assert "change_scale" not in relationship["properties"]
    assert "elapsed_game_days" not in relationship["properties"]

    section = schema["components"]["schemas"]["NovelDraftSection"]
    assert "knowledge" in section["properties"]["section_name"]["enum"]


def test_dynamic_fastapi_openapi_exposes_the_same_30_actions():
    dynamic = app.openapi()
    assert operation_ids(dynamic) == EXPECTED_ACTIONS


def test_removed_gate_actions_stay_out_of_gpt_surface():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    ids = operation_ids(schema)
    for removed in {
        "getAuditSnapshot",
        "getAuditSnapshotChunk",
        "commitAudit",
        "prepareCharacterKnowledgeRead",
        "getCharacterKnowledgeChunk",
        "getSceneKnowledgeReadStatus",
        "getCharacterBundle",
        "getCharacterMemory",
        "getTurnRange",
    }:
        assert removed not in ids


def test_action_descriptions_stay_under_custom_gpt_limit():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            description = str(operation.get("description") or "")
            assert len(description) <= 300, f"{method.upper()} {path} description is {len(description)} chars"


def test_custom_gpt_instruction_matches_rules_driven_transport():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert len(text) <= 8000
    for required in (
        "runtime_rules",
        "scene_builder",
        "saveNovelDraftSection",
        "appendDraftIntakeChunk",
        "updateDraftIntakeMapping",
        "confirmDraftReconciliation",
        "finalizeNovelDraft",
        "prepareDraftRead",
        "setDraftLaunchState",
        "prepareTurn",
        "getTurnPacketChunk",
        "prepareCharacterBundleRead",
        "getCharacterBundleChunk",
        "commitTurn",
        "resumeSession",
        "rollbackLastTurn",
        "prepareContinuationCompaction",
        "createContinuationSession",
        "knowledge_journal_add",
        "request_id",
        "opening_scene=true",
        "user_input=\"\"",
        "packet_id",
        "knowledge_reviewed=true",
    ):
        assert required in text

    for removed in (
        "knowledge_review_capable",
        "complete_knowledge_read_capable",
        "relationship_review_capable",
        "runtime_contract_capable",
        "strict_knowledge_capable",
        "getSceneKnowledgeReadStatus",
        "prepareCharacterKnowledgeRead",
        "getCharacterKnowledgeChunk",
        "commitAudit",
        "relationship_reviewed=true",
    ):
        assert removed not in text

    assert "стартовые знания" in text
    assert "turn=0" in text
    assert "Простое упоминание отсутствующего персонажа" in text
    assert "показывай `session_id`" in text
    assert "переноса/продолжения сессии" in text
    assert "Не показывай `packet_id`" in text


def test_static_schema_avoids_actions_parser_traps():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    assert schema["openapi"] == "3.1.0"

    def walk(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert "properties" in node, f"object schema without properties at {path}"
                assert isinstance(node["properties"], dict), f"properties must be an object at {path}"
            if node.get("type") == "array":
                assert "items" in node, f"array schema without items at {path}"
                assert isinstance(node["items"], dict), f"items must be a schema object at {path}"
                assert node["items"] != {}, f"empty array item schema at {path}"
            assert "const" not in node, f"const is intentionally avoided for Actions compatibility at {path}"
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(schema)

    schemas = schema["components"]["schemas"]
    refs = []

    def collect_refs(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                refs.append(ref.rsplit("/", 1)[-1])
            for value in node.values():
                collect_refs(value)
        elif isinstance(node, list):
            for value in node:
                collect_refs(value)

    collect_refs(schema)
    assert set(refs) <= set(schemas)
