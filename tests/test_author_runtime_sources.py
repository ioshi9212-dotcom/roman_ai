"""Protect ownership of active instructions without locking an author's prose."""
from pathlib import Path

from app.runtime_access import author_runtime_sources, runtime_documents, runtime_manifest, runtime_chunk

ROOT = Path(__file__).resolve().parents[1]


def test_active_runtime_is_exactly_the_two_author_documents():
    active = runtime_documents()
    assert active == author_runtime_sources()
    assert set(active) == {"rules", "scene_builder"}
    for key, content in active.items():
        name = "rules.md" if key == "rules" else "scene_builder.md"
        assert content == (ROOT / "runtime" / name).read_text(encoding="utf-8")
    assert {p.name for p in (ROOT / "runtime").glob("*.md")} == {"rules.md", "scene_builder.md"}


def test_runtime_chunks_ship_the_same_documents_without_a_second_contract():
    import json
    manifest = runtime_manifest()
    payload = json.loads("".join(runtime_chunk(i)["content"] for i in range(manifest["chunk_count"])))
    assert payload["documents"] == runtime_documents()
    assert payload["runtime_revision"] == manifest["runtime_revision"]
    assert "hard_checks" not in payload


def test_scene_direction_has_one_owner():
    docs = runtime_documents()
    instructions = (ROOT / "gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    for phrase in ("Мысли POV полностью приватны", "Если POV выбрал ждать",
                   "Завершён должен быть игровой ход, а не обязательно действие",
                   "Что я могу подумать:", "Состояние: {до 10 слов"):
        assert phrase in docs["scene_builder"]
        assert phrase not in docs["rules"]
        assert phrase not in instructions


def test_templates_do_not_reintroduce_retired_setup_or_audit_gates():
    import json
    for path in (ROOT / "state").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        note = value.get("_памятка", "")
        for obsolete in ("Стартовая сверка ОБЯЗАТЕЛЬНА", "точной уникальной цитатой",
                         "Каждые 15 ходов сверяй", "каждые 60 — сжимай"):
            assert obsolete not in note, path.name
    assert "стартовая_сверка" not in json.loads((ROOT / "state/отношения.json").read_text())


def test_bootstrap_installs_only_setup_adapters_and_one_gameplay_pipeline():
    import ast
    module = ast.parse((ROOT / "app/__init__.py").read_text())
    calls = [node.value.func.id for node in module.body
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
             and isinstance(node.value.func, ast.Name)]
    assert calls == ["_install_foundation_coverage", "_install_draft_intake",
                     "_install_setup_draft_v3", "_install_simple_setup", "_install_turn_pipeline"]
