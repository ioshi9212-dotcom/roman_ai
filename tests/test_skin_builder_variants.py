"""Independent live author builders: transport, revision and presentation."""
import json

from app import runtime_access, session_migrations, session_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request
from app.scene_header import parse_scene_header
from app.scene_format import validate_scene_output


def _new_session():
    return storage.create_session({
        "novel_id": "builder-variant-test",
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


def _context(sid):
    packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
    return packet, json.loads("".join(packet["chunks"]))


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "github")
    docs = {
        key: (runtime_access.RUNTIME_DIR / filename).read_text(encoding="utf-8")
        for key, filename in runtime_access.ACTIVE_RUNTIME_FILES.items()
    }
    monkeypatch.setattr(runtime_access, "_fetch_github_runtime_file", docs.__getitem__)
    return docs


def test_variants_share_rules_but_load_only_selected_builder(monkeypatch, tmp_path):
    docs = _setup(monkeypatch, tmp_path)
    with runtime_access.runtime_read_view("v2"):
        assert runtime_access.runtime_documents() == {
            "rules": docs["rules"], "scene_builder": docs["scene_builder_v2"],
        }
        revision = runtime_access.runtime_revision()
        docs["scene_builder_v3"] += "\nV3 edit"
        assert runtime_access.runtime_revision_for_builder("v2") == revision
    assert runtime_access.selected_builder_version() == "orig"
    with runtime_access.runtime_read_view("v3"):
        assert runtime_access.runtime_documents()["scene_builder"] == docs["scene_builder_v3"]
    with runtime_access.runtime_read_view("v2"):
        assert runtime_access.runtime_revision() == revision


def test_chat_variants_have_independent_packets_and_commit_revision(monkeypatch, tmp_path):
    docs = _setup(monkeypatch, tmp_path)
    a, b = _new_session(), _new_session()
    v2 = prepare_turn_request(a, "ход A", "request-a", builder_version="v2")
    v3 = prepare_turn_request(b, "ход B", "request-b", builder_version="v3")
    for sid, result, variant in ((a, v2, "v2"), (b, v3, "v3")):
        packet, context = _context(sid)
        assert result["builder_version"] == packet["builder_version"] == variant
        assert context["builder_version"] == variant
        assert context["scene_builder"] == docs[f"scene_builder_{variant}"]
        assert context["runtime_rules"] == docs["rules"]
        assert runtime_access.BUILDER_LABELS[variant] in context["builder_header_instruction"]
    docs["scene_builder_v3"] += "\nNew V3 rule."
    retry_a = prepare_turn_request(a, "ход A", "request-a", builder_version="v2")
    retry_b = prepare_turn_request(b, "ход B", "request-b", builder_version="v3")
    assert retry_a["packet_id"] == v2["packet_id"]
    assert retry_b["packet_id"] != v3["packet_id"]
    assert session_migrations.packet_is_current(_context(a)[0])
    assert _context(b)[1]["scene_builder"].endswith("New V3 rule.")
    for index in range(1, retry_a["chunk_count"]):
        storage.get_turn_packet_chunk(a, retry_a["packet_id"], index)
    def fake_commit(sid, payload):
        assert sid == a
        assert runtime_access.selected_builder_version() == "v2"
        assert runtime_access.runtime_documents()["scene_builder"] == docs["scene_builder_v2"]
        return {"ok": True}
    monkeypatch.setattr(session_runtime, "commit_turn", fake_commit)
    assert commit_turn_request(a, {
        "packet_id": retry_a["packet_id"], "user_input": "ход A",
        "scene_output": "scene",
        "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True},
    })["ok"]
    assert runtime_access.selected_builder_version() == "orig"


def test_explicit_switch_rebuilds_pending_without_story_mutation(monkeypatch, tmp_path):
    docs = _setup(monkeypatch, tmp_path)
    sid = _new_session()
    first = prepare_turn_request(sid, "тот же ход", "request-1", builder_version="v2")
    second = prepare_turn_request(sid, "тот же ход", "request-1", builder_version="v3")
    assert first["packet_id"] != second["packet_id"]
    assert _context(sid)[1]["scene_builder"] == docs["scene_builder_v3"]
    assert storage._read_turns(storage.SESSIONS_DIR / sid) == []
    retry = prepare_turn_request(sid, "тот же ход", "request-1")
    assert retry["packet_id"] == second["packet_id"]
    assert retry["builder_version"] == "v3"
    orig = prepare_turn_request(_new_session(), "новый ход", "request-orig")
    assert orig["builder_version"] == "orig"
    assert "Orig" in orig["instruction"]


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
