from app.runtime_access import runtime_documents, runtime_manifest, runtime_payload


def test_runtime_keeps_pov_agency_without_separate_contract_document():
    docs = runtime_documents()
    rules = docs["rules"]
    builder = docs["scene_builder"]
    assert "POV — живой участник, не камера" in rules
    assert "Не превращай его в мебель" in builder
    assert "Значимые решения оставляй игроку" in builder
    assert "pov_contract" not in runtime_payload()["documents"]


def test_scene_builder_keeps_selective_cinematic_behavior_in_plain_language():
    builder = runtime_documents()["scene_builder"]
    assert "Пиши конкретно и визуально" in builder
    assert "Подробно только важное" in builder
    assert "Сохраняй геометрию" in builder
    assert "Важное показывай подробно" in builder
    assert "Понятную дорогу, ожидание" in builder


def test_runtime_version_marks_writer_first_two_document_runtime():
    manifest = runtime_manifest()
    assert manifest["runtime_version"] == "2.0.0-writer-first"
    assert manifest["chunk_count"] >= 1
    assert manifest["chunk_count"] <= 3
