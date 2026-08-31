from app.runtime_access import runtime_documents, runtime_manifest


def test_runtime_contains_active_pov_contract():
    docs = runtime_documents()
    assert "pov_contract" in docs
    contract = docs["pov_contract"]
    assert "POV — полноценный участник, а не камера и не мебель" in contract
    assert "POV НЕ должен искусственно молчать" in contract
    assert "не ограничивай POV одной репликой на весь ход" in contract
    assert "Такие реплики не обязаны быть однословными" in contract
    assert "не требуют остановки сцены" in contract


def test_runtime_version_bumped_for_pov_contract():
    manifest = runtime_manifest()
    assert manifest["runtime_version"] == "1.7.1"
    assert manifest["chunk_count"] >= 1
