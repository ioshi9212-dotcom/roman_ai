import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.choice_mcp import RESOURCE
from app.main import app
from app.scene_choices import parse_choices
from app.scene_header import parse_scene_header
from app.session_recovery import _parse_scene_header, _speaker_present_ids
from app.stability_runtime import _scene_header_current


SCENE = """**◈ Проверка** · осень
◷ День 2 · четверг, 22.10.2026 · 14:20
⌖ мастерская · ☁︎ ясно
▸ Разговор у верстака
────────────────
◇ **Рина** · свитер · хвост
▣ При себе: телефон

> **Рен** — Продолжим?

Что я могу сделать:
1. Подойти.
2. Сесть.
3. Выйти.

Что я могу сказать:
1. Продолжай.
2. Нет.
3. Почему?

Что я могу подумать:
1. Интересно.
2. Странно.
3. Посмотрим.

Состояние: спокойно
Отношения:
Рен - доверие 25/0
Ход 2 · цикл 2/15"""


@pytest.mark.parametrize("text", [SCENE, SCENE.replace(
    "◷ День 2 · четверг, 22.10.2026 · 14:20\n⌖ мастерская · ☁︎ ясно",
    "🕒 День 2 · четверг, 22.10.2026, 14:20 ·\n📍 мастерская 🌦️ Погода: ясно")])
def test_commit_and_recovery_read_both_header_formats(text):
    expected = {"date": "22.10.2026", "time": "14:20", "game_day": 2, "location": "мастерская"}
    assert _scene_header_current(text) == expected
    assert _parse_scene_header(text) == dict(expected, scene="Разговор у верстака")
    assert parse_scene_header(None) == {}


def test_quoted_dialogue_is_visible_to_recovery():
    assert _speaker_present_ids(SCENE, cards=[{"id": "ren", "name": "Рен"}]) == ["ren"]


def test_missing_choices_are_not_fabricated():
    with pytest.raises(ValueError, match="SCENE_CHOICES_INCOMPLETE"):
        parse_choices(SCENE.replace("3. Посмотрим.", ""))


def test_mcp_reads_saved_choices_without_creating_turn_or_exposing_private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path)
    sid = str(uuid4())
    root = tmp_path / sid
    root.mkdir()
    (root / "turns.jsonl").write_text(json.dumps({"turn_number": 2, "scene_output": SCENE}, ensure_ascii=False) + "\n")
    (root / "state.json").write_text('{"private_secret":"НЕ ВЫДАВАТЬ"}')
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    with TestClient(app) as client:
        def rpc(method, params):
            response = client.post("/mcp/", headers={"Accept": "application/json, text/event-stream"},
                                   json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            assert response.status_code == 200, response.text
            return response.json()["result"]
        init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}})
        assert "tools" in init["capabilities"]
        tools = rpc("tools/list", {})["tools"]
        assert {t["name"] for t in tools} == {"show_scene_choices", "check_scene_choices"}
        assert all(t["annotations"]["readOnlyHint"] for t in tools)
        assert tools[0]["_meta"]["ui"]["resourceUri"] == RESOURCE
        resource = rpc("resources/read", {"uri": RESOURCE})["contents"][0]
        assert resource["mimeType"] == "text/html;profile=mcp-app"
        assert "/* CHOICE_APP */" not in resource["text"]
        assert resource["_meta"]["ui"]["csp"]["connectDomains"] == []
        result = rpc("tools/call", {"name": "show_scene_choices", "arguments": {
            "session_id": sid, "expected_turn": 2}})
        data = result["structuredContent"]
        assert data["session_id"] == sid
        assert [len(g["items"]) for g in data["groups"]] == [3, 3, 3]
        assert "НЕ ВЫДАВАТЬ" not in json.dumps(result, ensure_ascii=False)
        for session_id, turn in ((sid, 1), ("../outside", 2)):
            error = rpc("tools/call", {"name": "show_scene_choices", "arguments": {
                "session_id": session_id, "expected_turn": turn}})
            assert error["isError"] is True
        checked = rpc("tools/call", {"name": "check_scene_choices", "arguments": {
            "session_id": sid, "expected_turn": 2, "expected_scene_sha256": data["scene_sha256"]}})
        assert checked["structuredContent"]["current"] is True
        stale = rpc("tools/call", {"name": "check_scene_choices", "arguments": {
            "session_id": sid, "expected_turn": 2, "expected_scene_sha256": "0" * 64}})
        assert stale["isError"] is True
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_instruction_limit_including_crlf_and_npc_rules_preserved():
    root = Path(__file__).resolve().parents[1]
    text = (root / "gpt/custom_gpt_instructions.md").read_text()
    assert len(text.replace("\n", "\r\n")) <= 8000
    builder = (root / "runtime/scene_builder.md").read_text()
    assert "Запрещено делать их понимающими границы. Они могут действовать не удобно и неправильно." in builder
    assert "**3. Запрет принудительного смягчения.**" in builder


def test_multiline_choice_is_not_truncated():
    groups = parse_choices(SCENE.replace("1. Подойти.", "1. Подойти.\n   Открыть дверь."))
    assert groups[0]["items"][0]["text"] == "Подойти.\n   Открыть дверь."
