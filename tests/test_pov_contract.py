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


def test_scene_builder_contains_selective_cinematic_coverage_contract():
    builder = runtime_documents()["scene_builder"]
    assert "# CINEMATIC COVERAGE CONTRACT" in builder
    assert "ROUTINE" in builder
    assert "STANDARD" in builder
    assert "CINEMATIC" in builder
    assert "Ощущения НЕ заменяют визуал" in builder
    assert "NO MISSING BEATS" in builder
    assert "Не делай ранний монтажный обрыв" in builder
    assert "НЕ ПЕРЕОПИСЫВАЙ РУТИНУ" in builder
    assert "ВАЖНЫЙ МОМЕНТ НУЖНО НЕ ТОЛЬКО ПОНЯТЬ, НО И УВИДЕТЬ" in builder
    assert "`character_memory[character_id]`" in builder
    assert "memory_full.characters[character_id]" not in builder


def test_runtime_version_bumped_for_working_context_contract():
    manifest = runtime_manifest()
    assert manifest["runtime_version"] == "1.9.0"
    assert manifest["chunk_count"] >= 1
