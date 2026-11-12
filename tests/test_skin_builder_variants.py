"""Single active author builder; historical alternate headers remain readable."""
import json

from app import runtime_access, session_migrations, storage
from app.operation_service import prepare_turn_request
from app.scene_header import parse_scene_header
from app.scene_format import validate_scene_output


def test_only_original_builder_is_live_and_reserves_remain(monkeypatch):
    files = runtime_access.RUNTIME_DIR
    assert (files / "scene_builder_v2.md").is_file()
    assert (files / "scene_builder_v3.md").is_file()
    calls = []
    def fetch(key):
        calls.append(key)
        return (files / runtime_access.ACTIVE_RUNTIME_FILES[key]).read_text(encoding="utf-8")
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "github")
    monkeypatch.setattr(runtime_access, "_fetch_github_runtime_file", fetch)
    with runtime_access.runtime_read_view():
        docs = runtime_access.runtime_documents()
        assert docs == {
            "rules": (files / "rules.md").read_text(encoding="utf-8"),
            "scene_builder": (files / "scene_builder.md").read_text(encoding="utf-8"),
        }
    assert sorted(calls) == ["rules", "scene_builder"]


def test_old_session_selection_is_ignored_without_resetting_session(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "file")
    sid = storage.create_session({
        "novel_id": "single-builder-test",
        "title": "Test",
        "novel": {"pov_character": "pov"},
        "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026", "time": "10:00",
                "location": "room", "present_characters": ["pov"],
            },
        },
    })["session_id"]
    root = storage.SESSIONS_DIR / sid
    reserve_settings = root / ".runtime" / "builder_settings.json"
    storage._write_json(reserve_settings, {"builder_version": "v3"})
    before_meta = storage._read_json(root / "meta.json", {})
    first = prepare_turn_request(sid, "старый ход", "old-request")
    packet = storage._read_json(root / "turn_packet.json", {})
    # Emulate a pending packet issued by the alternate runtime before deployment.
    packet["runtime_revision"] = "obsolete-v3-revision"
    packet["builder_version"] = "v3"
    storage._write_json(root / "turn_packet.json", packet)
    result = prepare_turn_request(sid, "старый ход", "old-request")
    assert result["packet_id"] != first["packet_id"]
    saved = storage._read_json(root / "turn_packet.json", {})
    context = json.loads("".join(saved["chunks"]))
    assert context["scene_builder"] == (runtime_access.RUNTIME_DIR / "scene_builder.md").read_text(encoding="utf-8")
    assert "builder_version" not in context
    assert "builder_header_instruction" not in context
    assert storage._read_json(reserve_settings, {}) == {"builder_version": "v3"}
    assert storage._read_json(root / "meta.json", {})["turn_number"] == before_meta["turn_number"]
    assert storage._read_turns(root) == []
    abandoned = storage._read_json(root / "abandoned_turn_packets.json", [])
    assert abandoned[-1]["packet_id"] == first["packet_id"]
    assert abandoned[-1]["user_input"] == "старый ход"
    assert session_migrations.packet_is_current(saved)


def test_v2_v3_headers_parse_and_validate():
    headers = (
        "**◈ Test · V2** · лето\n◷ День 1 · вторник, 01.09.2026 · 10:00\n⌖ room · ☁︎ ясно\n▸ разговор\n◇ **POV** · Одежда, волосы: джинсы\n▣ При себе: ключи\n────────────────",
        "📖 Test · лето · V3\n🗓️ День 1 · вторник, 01.09.2026, 10:00\n📌 room · 🌤️ Погода: ясно\n🎬 Сцена: разговор\n👤 POV\n👕 Одежда, волосы: джинсы\n🎒 Инвентарь: ключи\n--------------------------------------------------------",
    )
    footer = "\nPOV смотрит в окно.\n\nЧто я могу сделать:\n1. Войти.\n2. Выйти.\n3. Подойти.\n\nЧто я могу сказать:\n1. Да.\n2. Нет.\n3. Привет.\n\nЧто я могу подумать:\n1. Странно.\n2. Тихо.\n3. Поздно.\n\nСостояние: спокойно\nОтношения:\nХод 1 · цикл 1/15"
    for header in headers:
        scene = header + footer
        assert parse_scene_header(scene) == {
            "date": "01.09.2026", "time": "10:00", "game_day": 1,
            "location": "room", "scene": "разговор",
        }
        assert validate_scene_output(scene) == scene

