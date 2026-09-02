import json
import tempfile
from pathlib import Path

from app import storage
from app.session_recovery import recover_session_current


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_legacy_turn_without_roster_or_relationship_footer_recovers_from_saved_scene_actors():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "legacy-current",
            "title": "Legacy Current",
            "novel": {"pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Рина", "is_pov": True},
                {"character_id": "adrian", "name": "Эдриан"},
                {"character_id": "chloe", "name": "Хлоя"},
            ],
            "starting_state": {
                "pov": {"character_id": "rina"},
                "current": {"date": "03.09.2026", "time": "10:00", "location": "дом"},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        scene = """🎭 Legacy Current · осень
🕒 День 2 · четверг, 03.09.2026, 14:20 · 📍 мастерская
🌦️ Погода: ясно
⚙️ Сцена: разговор у верстака
✦ Рина
🧥 Одежда, волосы: обычно
◈ Инвентарь: телефон
--------------------------------------------------------

Эдриан опёрся ладонью о край верстака и посмотрел на Рину.

**Эдриан** — Ты серьёзно?

Хлоя остановилась у двери, всё ещё держа куртку в руке.

**Хлоя** — Я вообще-то всё слышу.

Что я могу сделать:
1. Ответить.
2. Осмотреться.
3. Подойти ближе.

Что я могу сказать:
1. Да.
2. И что?
3. Ладно.

Что я могу подумать:
1. Весело.
2. Прекрасно.
3. Надо кофе.

Состояние: спокойно
Отношения:

Ход 44 · цикл 14/15"""
        entry = {
            "turn_number": 44,
            "saved_at": "2026-09-02T12:00:00+00:00",
            "user_input": "test",
            "scene_output": scene,
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
                "state_patch": {
                    "current": {
                        "date": "03.09.2026",
                        "time": "14:20",
                        "location": "мастерская",
                        "scene": "разговор у верстака",
                    }
                },
            },
        }
        (root / "turns.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 44
        meta["last_audit_turn"] = 30
        meta["audit_required"] = False
        storage._write_json(root / "meta.json", meta)
        state = storage._read_json(root / "state.json", {})
        state["current"] = {
            "date": "03.09.2026",
            "time": "14:20",
            "location": "мастерская",
            "scene": "разговор у верстака",
            "present_characters": [],
        }
        state["characters"] = {}
        storage._write_json(root / "state.json", state)

        repaired = recover_session_current(sid)
        assert repaired["turn_number"] == 44
        assert repaired["turn_created"] is False
        assert set(repaired["current"]["present_characters"]) == {"rina", "adrian", "chloe"}
        assert repaired["provenance"]["present_source"] == "turn:44:scene_evidence"
        assert set(repaired["provenance"]["latest_scene_speakers"]) == {"adrian", "chloe"}
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 44
