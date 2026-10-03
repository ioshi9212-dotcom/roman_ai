from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from app.runtime_access import runtime_documents, runtime_manifest, runtime_payload


def test_runtime_keeps_pov_agency_without_separate_contract_document():
    docs = runtime_documents()
    rules = docs["rules"]
    builder = docs["scene_builder"]
    assert "POV — живой участник, не камера" in rules
    assert "обычные низкорисковые реплики" in rules
    assert "За игроком остаются новые значимые решения POV" in rules
    assert "POV остаётся живым участником" in builder
    assert "pov_contract" not in runtime_payload()["documents"]


def test_scene_builder_uses_content_based_detail_without_mode_machine():
    builder = runtime_documents()["scene_builder"]
    assert "Важное физическое действие должно быть понятно глазами" in builder
    assert "Не нужен покадровый каталог микродвижений" in builder
    assert "Рутину, повтор и ожидание сжимай" in builder
    assert "2000–2400 непробельных символов" in builder
    assert "2500–3000 непробельных символов" in builder
    assert "ROUTINE" not in builder
    assert "STANDARD" not in builder
    assert "CINEMATIC" not in builder


def test_runtime_version_marks_rules_scene_builder_runtime():
    manifest = runtime_manifest()
    assert manifest["runtime_version"] == "3.1.0-rules-scene-builder"
    assert manifest["chunk_count"] >= 1
    assert manifest["chunk_count"] <= 3


def test_first_scene_start_command_is_control_not_pov_speech():
    rules = runtime_documents()["rules"]
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "opening_scene.active=true" in rules
    assert "пользователь ещё ничего не сказал и не сделал" in rules
    assert "запускай первую сцену" in instructions
    assert "служебная команда, не речь POV" in instructions
