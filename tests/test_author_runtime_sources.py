import hashlib
from pathlib import Path

from app.runtime_access import author_runtime_sources, runtime_documents


ROOT = Path(__file__).resolve().parents[1]


AUTHOR_BLOB_SHA = {
    "rules.md": "edad7e804e7a6a31d3a66f81bc2a5dd9a4c04db2",
    "scene_builder.md": "3f62718c8692202c10e43ed6d325f3921758ae92",
}


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    return hashlib.sha1(payload).hexdigest()


def test_author_runtime_sources_are_byte_locked():
    for name, expected in AUTHOR_BLOB_SHA.items():
        assert _git_blob_sha(ROOT / "runtime" / name) == expected


def test_active_runtime_is_exactly_the_two_author_documents():
    author = author_runtime_sources()
    active = runtime_documents()

    assert set(active) == {"rules", "scene_builder"}
    assert active == author
    assert active["rules"] == (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert active["scene_builder"] == (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")


def test_turn_boundary_does_not_force_interaction_to_end():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")

    assert "сформируй законченную сцену" not in rules
    assert "Завершён должен быть игровой ход, а не обязательно действие" in rules
    assert "Развитие не равно завершению" in builder
    assert "«Поцеловать», «обнять»" in builder
    assert "не добавляй искусственную развязку" in builder
    assert "не доводи его до окончания ради перехода к вариантам" in builder
    assert "не вынуждай персонажей разговаривать посреди физического момента" in builder
    assert "не принимай" not in builder

def test_recurring_npcs_can_reenter_without_forced_events():
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Постоянные NPC не исчезают после разговора, примирения или отказа" in builder
    assert "Если сцена вязнет в бытовухе и повторах" in builder
    assert "Не вводи NPC по таймеру" in builder
    assert "Даже формально решённый вопрос" in builder
    assert "Возвращай эти линии по причинности" in builder
