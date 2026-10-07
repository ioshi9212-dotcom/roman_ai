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
    audit_commit = schema["components"]["schemas"]["AuditCommit"]
    assert set(audit_commit["required"]) == {"audit_id", "start_turn", "end_turn"}

    commit_request = schema["components"]["schemas"]["CommitTurnRequest"]
    assert commit_request["type"] == "object"
    assert {"packet_id", "user_input", "scene_output", "extracted"} <= set(commit_request["properties"])
    assert {"audit_id", "start_turn", "end_turn", "repairs", "notes"} <= set(commit_request["properties"])

    commit_action = schema["paths"]["/sessions/{session_id}/turns"]["post"]
    commit_schema = commit_action["requestBody"]["content"]["application/json"]["schema"]
    assert commit_schema == {"$ref": "#/components/schemas/CommitTurnRequest"}
    assert "knowledge_participants" not in schema["components"]["schemas"]["ChronologyItem"]["properties"]
    dumped = str(turn_commit)
    assert "knowledge_reviewed" not in dumped
    assert "runtime_contract_version" not in dumped
    extracted = schema["components"]["schemas"]["TurnExtracted"]
    assert "scene_builder_reviewed" in extracted["properties"]
    assert "persistence_reviewed" in extracted["properties"]
    assert "knowledge_reviewed" in extracted["properties"]
    assert "relationship_reviewed" not in extracted["properties"]
    assert "relationship_review" in extracted["properties"]

    relationship = schema["components"]["schemas"]["RelationshipUpdate"]
    assert "dynamic" in relationship["properties"]
    assert relationship["properties"]["change_scale"]["enum"] == ["ordinary", "critical_event"]
    assert "elapsed_game_days" not in relationship["properties"]

    knowledge_text = schema["components"]["schemas"]["KnowledgeJournalAdd"]["properties"]["text"]["description"]
    assert "minimum information actually received or learned" in knowledge_text
    assert "source-qualified" in knowledge_text
    assert "unstated time, place, person" in knowledge_text

    section = schema["components"]["schemas"]["NovelDraftSection"]
    assert {"knowledge", "locations", "canon_notes"} <= set(section["properties"]["section_name"]["enum"])

    current = schema["components"]["schemas"]["CurrentState"]["properties"]
    for field in ("positions", "scene_items", "unfinished_actions", "remote_channels", "location_id", "zone_id"):
        assert field in current

    state_patch = schema["components"]["schemas"]["StatePatch"]["properties"]
    assert {"current", "pov", "characters"} <= set(state_patch)
    runtime_character = schema["components"]["schemas"]["RuntimeCharacterState"]["properties"]
    assert {"location", "zone", "clothing", "inventory"} <= set(runtime_character)


def test_dynamic_fastapi_openapi_exposes_the_same_30_actions():
    dynamic = app.openapi()
    assert operation_ids(dynamic) == EXPECTED_ACTIONS

    commit_schema = dynamic["paths"]["/sessions/{session_id}/turns"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert commit_schema == {"$ref": "#/components/schemas/CommitTurnRequest"}
    assert dynamic["components"]["schemas"]["CommitTurnRequest"]["type"] == "object"


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
    assert len(text) + text.count("\n") <= 8000  # CRLF-safe editor budget
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
        'user_input=""',
        "packet_id",
        "knowledge_reviewed=true",
        "relationships.json",
        "change_scale=critical_event",
    ):
        assert required in text

    for removed in (
        "knowledge_review_capable",
        "complete_knowledge_read_capable",
        "relationship_review_capable",
        "relationship_reviewed=true",
        "runtime_contract_capable",
        "strict_knowledge_capable",
        "getSceneKnowledgeReadStatus",
        "prepareCharacterKnowledgeRead",
        "getCharacterKnowledgeChunk",
        "commitAudit",
        "STORY_PROGRESS_REQUIRED",
        "narrative_guardrails",
        "story_drive",
    ):
        assert removed not in text

    assert "стартовые знания" in text
    assert "turn=0" in text
    assert "Cast registry — активный каст." in text
    assert "Не жди POV, intent/thread или удобного момента" in text
    assert "Chronology и personal knowledge независимы" in text
    assert "required_audit" in text
    assert "bundle проверяет знания, не разрешение" in text
    assert "показывай `session_id`" in text
    assert "`packet_id`, `read_id`, chunk-статусы и сверки не показывай" in text
    assert "первый видимый текст = сама сцена" in text
    assert "не выводи планы/анализ/пояснения" in text
    assert "Техпояснения только на техвопрос" in text
    assert "не смягчай, не обобщай" in text
    assert "Все постоянные персонажи из RAW" in text
    assert "включая важных offscreen/nearby" in text
    assert "**Location profile:**" in text
    assert "`locations`" in text
    assert "`canon_notes`" in text
    assert "`location_context` только текущего физического места" in text
    assert "`recovery_required`" in text
    assert "`last_saved_chunk`" in text
    assert "Новый материал → новый `block_id`" in text


def test_runtime_contract_file_stays_removed_and_transport_lives_in_active_docs():
    assert not (ROOT / "runtime" / "runtime_contract.md").exists()
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert "prepareCharacterBundleRead" in instructions
    assert "getCharacterBundleChunk" in instructions
    assert "prepareCharacterKnowledgeRead" not in instructions + rules
    assert "getCharacterKnowledgeChunk" not in instructions + rules
    assert "getSceneKnowledgeReadStatus" not in instructions + rules


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
