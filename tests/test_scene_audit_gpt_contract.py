from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_custom_gpt_instructions_include_scene_audit_compaction_and_fit_editor_limit():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert len(text) <= 8000
    for phrase in (
        "repairs.scene_compactions",
        "15 ходов одной сцены = ОДНА запись",
        "repairs.memory_compactions",
        "не закрывай информационную дыру задним числом",
        "до следующего значимого выбора",
        "очевидную орфографию",
    ):
        assert phrase in text


def test_openapi_exposes_scene_compaction_and_stays_within_custom_gpt_action_limit():
    spec = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    paths = spec["paths"]
    assert "/health" not in paths

    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    operations = sum(
        1
        for path_item in paths.values()
        for method in path_item
        if method.lower() in methods
    )
    assert operations == 30

    schemas = spec["components"]["schemas"]
    audit_commit = schemas["AuditCommit"]
    assert "repairs" in audit_commit["required"]
    assert audit_commit["properties"]["repairs"]["$ref"].endswith("/AuditRepairs")

    repairs = schemas["AuditRepairs"]
    assert repairs["required"] == ["scene_compactions"]
    assert repairs["properties"]["scene_compactions"]["minItems"] == 1
    assert repairs["properties"]["scene_compactions"]["items"]["$ref"].endswith("/SceneCompaction")
    assert repairs["properties"]["memory_compactions"]["items"]["$ref"].endswith("/MemoryCompaction")

    scene = schemas["SceneCompaction"]
    assert scene["properties"]["summary"]["minLength"] == 60
    assert scene["properties"]["summary"]["maxLength"] == 1200
    assert scene["properties"]["status"]["enum"] == ["open", "closed"]

    memory = schemas["MemoryCompaction"]
    assert memory["properties"]["memory_type"]["enum"] == [
        "knowledge",
        "experiences",
        "dialogue_memory",
    ]
    assert memory["properties"]["summary"]["maxLength"] == 900
