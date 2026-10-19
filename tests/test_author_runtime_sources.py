"""Contract checks against the current author-approved runtime documents."""
import hashlib
from pathlib import Path

from app.runtime_access import author_runtime_sources, runtime_documents


ROOT = Path(__file__).resolve().parents[1]

# The author's two documents are byte-locked. These are the current blobs from
# main, not phrases retained from an older, superseded Skin Builder revision.
AUTHOR_BLOB_SHA = {
    "rules.md": "001ddf3f5906c91fc4a68e5e15f38b54b09944e2",
    "scene_builder.md": "e48d2a4d1ca8bc57e2d3cf1a8299ef186deedf9c",
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
    assert "Завершён должен быть игровой ход, а не обязательно действие" in rules
    assert "Сцены могут быть разной плотности" in builder
    assert "Обычное продолжение уже выбранного действия выполняй в самой сцене." in builder
    assert "Не пропускать важные сцены" in builder
    assert "сформируй законченную сцену" not in rules


def test_recurring_npcs_can_reenter_without_forced_events():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Не удаляй NPC из present_characters до реального leave." in rules
    assert "Незакрытая линия остаётся активной" in rules
    assert "ПОВ - не центр мира" in builder
    assert "Самостоятельно развивай мир и создавай новых персонажей" in builder
    assert "Cast registry даёт контекст" in rules or "cast_registry" in rules


def test_waiting_is_not_a_repeatable_static_turn():
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Если ПОВ ничего не делает, мир и другие НПС будут двигать сюжет сами" in builder
    assert "Пропускать только скучную рутину, которая в сцене ничего не меняет." in builder
    assert "Конец сцены: он всегда должен оставлять крючок." in builder


def test_npc_acts_independently_during_ongoing_contact():
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    assert "Никто не существует только ради POV." in builder
    assert "Реакция определяется характером, желаниями, чувствами и отношениями" in builder
    assert "Важные сцены, могут быть вообще без реплик" in builder
    assert "Романтические, интимные, чувственные" in builder
    assert "Завершён должен быть игровой ход, а не обязательно действие" in rules


def test_pov_viewpoint_and_external_camera_are_not_contradictory():
    builder = (ROOT / "runtime" / "scene_builder.md").read_text(encoding="utf-8")
    assert "Восприятие сцены привязано к POV" in builder
    assert "включая POV, описываются от третьего лица" in builder
    assert "От первого лица ПОВ. Но камера от третьего лица." not in builder
