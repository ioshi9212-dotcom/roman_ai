from pathlib import Path

from app import runtime_access


ROOT = Path(__file__).resolve().parents[1]


def test_scene_quality_rules_are_composed_into_runtime_rules_without_editing_source_rules():
    base_rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    quality_rules = (ROOT / "runtime" / "scene_quality_rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    runtime = runtime_access.runtime_documents()

    assert set(runtime) == {"rules", "scene_builder"}
    assert "# SCENE QUALITY RULES" not in base_rules
    assert "# SCENE QUALITY RULES" in quality_rules
    assert runtime["rules"].startswith(base_rules.rstrip())
    assert quality_rules.strip() in runtime["rules"]
    assert runtime["scene_builder"] == builder


def test_scene_quality_contract_blocks_empty_micro_choices_and_preserves_scene_types():
    rules = runtime_access.runtime_documents()["rules"]

    required = (
        "Если POV сейчас выберет любой разумный вариант следующего действия, изменится ли что-нибудь существенное?",
        "Если ответ «нет», это не значимый выбор.",
        "После этого хода история находится в содержательно другом состоянии, чем до него?",
        "Само по себе физическое перемещение",
        "## БЫТОВАЯ СЦЕНА",
        "## РОМАНТИЧЕСКАЯ СЦЕНА",
        "## ИНТИМНАЯ СЦЕНА",
        "## ЭКШН, БОЙ, ВОЙНА, ОПАСНОСТЬ",
        "## ТРИЛЛЕР И ТАЙНА",
        "## СВЕРХЪЕСТЕСТВЕННОЕ",
        "## КОНФЛИКТ И ДРАМА",
        "Есть ли у читателя причина хотеть следующий ход именно сейчас?",
        "Каждый ход должен либо приносить интересное содержательное изменение, либо сжимать путь до него.",
        "scene_progressed=true",
    )
    for phrase in required:
        assert phrase in rules


def test_runtime_surface_stays_two_documents_and_scene_quality_is_not_scene_builder():
    payload = runtime_access.runtime_payload()
    assert set(payload["documents"]) == {"rules", "scene_builder"}
    assert "SCENE QUALITY RULES" in payload["documents"]["rules"]
    assert "SCENE QUALITY RULES" not in payload["documents"]["scene_builder"]
    assert "scene purpose and choice boundaries" in payload["instruction"]
