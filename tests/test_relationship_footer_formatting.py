import tempfile
from pathlib import Path

from app import relationship_runtime, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "footer-format",
        "title": "Footer Format",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
            {"character_id": "jayden", "name": "Джейден"},
        ],
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {
                "date": "03.09.2026",
                "time": "14:20",
                "location": "база",
                "present_characters": ["elena", "jayden"],
            },
            "relationships": {"jayden": {"симпатия": 12, "настороженность": 8}},
        },
    }


def extracted():
    return {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def scene(row: str):
    return f"""🎭 Footer Format · осень
🕒 День 1 · четверг, 03.09.2026, 14:20 · 📍 база
🌦️ Погода: ясно
⚙️ Сцена: тест footer
✦ Елена
🧥 Одежда, волосы: обычно
◈ Инвентарь: телефон
--------------------------------------------------------

Джейден посмотрел на Елену.

Что я могу сделать:
1. Остаться.
2. Подойти.
3. Отойти.

Что я могу сказать:
1. Да.
2. Нет.
3. Ладно.

Что я могу подумать:
1. Хм.
2. Интересно.
3. Понятно.

Состояние: спокойно
Отношения:
{row}

Ход 1 · цикл 1/15"""


def test_parser_accepts_bold_name_and_em_dash():
    cards = novel()["characters"]
    parsed = relationship_runtime._parse_footer(
        scene("**Джейден** — симпатия 13/+1; настороженность 8/0"),
        cards=cards,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    assert parsed["jayden"][0]["label"] == "симпатия"
    assert parsed["jayden"][0]["value"] == 13
    assert parsed["jayden"][0]["delta"] == 1


def test_commit_accepts_bold_name_and_em_dash_footer_row():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "test")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("**Джейден** — симпатия 13/+1; настороженность 8/0"),
                "extracted": extracted(),
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["jayden"]["симпатия"] == 13
        assert state["relationships"]["jayden"]["настороженность"] == 8


def test_parser_accepts_en_dash_and_markdown_metric_label():
    cards = novel()["characters"]
    parsed = relationship_runtime._parse_footer(
        scene("- **Джейден** – **симпатия** 13/+1; настороженность 8/0"),
        cards=cards,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    assert [item["label"] for item in parsed["jayden"]] == ["симпатия", "настороженность"]
