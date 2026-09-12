import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage
from app.operation_service import commit_turn_request
from app.pov_participation_guard import validate_pov_participation


def _setup_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "pov_guard",
        "title": "POV Guard",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
            {"character_id": "liam", "name": "Лиам"},
            {"character_id": "aiden", "name": "Эйден"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {
                "date": "01.09.2026",
                "time": "12:00",
                "location": "база",
                "present_characters": ["elena", "liam", "aiden"],
            },
        },
    }


def _scene(body: str) -> str:
    return f"""🎭 POV Guard · осень
🕒 День 1 · вторник, 01.09.2026, 12:00 · 📍 база
🌦️ Погода: ясно
⚙️ Сцена: разговор
✦ Елена
🧥 Одежда, волосы: форма
--------------------------------------------------------
{body}

Что я могу сделать:
1. Остаться.
2. Подойти ближе.
3. Выйти.

Что я могу сказать:
1. Ответить.
2. Спросить.
3. Промолчать.

Что я могу подумать:
1. Оценить ситуацию.
2. Вспомнить разговор.
3. Решить, что делать дальше.

Состояние: спокойна
Отношения:
Лиам - доверие 10
Эйден - доверие 10

Ход 1 · цикл 1/15"""


def _reviewed():
    return {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
        "state_patch": {},
    }


def test_rejects_obvious_pov_furniture_in_multi_npc_dialogue():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        body = (
            "Елена остановилась у стола.\n\n"
            "**Лиам** — Первый длинный вопрос, адресованный ей.\n\n"
            "Он продолжил объяснять ситуацию, не отводя взгляда. " + "Подробность. " * 25 + "\n\n"
            "**Эйден** — Второй вопрос ей же.\n\n"
            "Разговор между ними продолжился. " + "Ещё деталь. " * 20 + "\n\n"
            "**Лиам** — Третья реплика, снова обращённая к ней.\n\n"
            "Они ещё некоторое время обсуждали происходящее между собой. " + "Фраза. " * 25
        )
        with pytest.raises(RuntimeError, match="POV_PARTICIPATION_REQUIRED"):
            validate_pov_participation(sid, _scene(body))


def test_allows_significant_choice_boundary_when_pov_remains_visibly_present():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        body = (
            "Елена остановилась у стола.\n\n"
            "**Лиам** — Ты действительно хочешь это сделать?\n\n"
            "Эйден коротко посмотрел на неё.\n\n"
            "**Эйден** — Решать тебе.\n\n"
            "Елена перевела взгляд с одного на другого, пальцы на секунду сжались на краю стола. "
            "Ответ здесь уже означал бы её решение, поэтому она задержалась на этой границе."
        )
        result = validate_pov_participation(sid, _scene(body))
        assert result["checked"] is True
        assert result["furniture_risk"] is False


def test_allows_active_pov_dialogue_through_scene():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        body = (
            "**Лиам** — Ты куда собралась?\n\n"
            "**Елена** — На кухню. Удивительно, правда?\n\n"
            "Лиам хмыкнул.\n\n"
            "**Эйден** — Возьми и мне кофе.\n\n"
            "Елена уже у двери обернулась через плечо.\n\n"
            "**Елена** — Мечтай."
        )
        result = validate_pov_participation(sid, _scene(body))
        assert result["pov_dialogue_lines"] == 2
        assert result["furniture_risk"] is False


def test_commit_rejection_does_not_consume_pending_packet_or_create_turn():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        raw_input = "(остановиться у стола)"
        packet = session_runtime.prepare_turn_packet(sid, raw_input)

        body = (
            "Елена остановилась у стола.\n\n"
            "**Лиам** — Первый вопрос.\n\n" + "Продолжение разговора. " * 35 + "\n\n"
            "**Эйден** — Второй вопрос.\n\n" + "Они говорят дальше. " * 35 + "\n\n"
            "**Лиам** — Третий вопрос."
        )
        payload = {
            "packet_id": packet["packet_id"],
            "user_input": raw_input,
            "scene_output": _scene(body),
            "extracted": _reviewed(),
        }

        with pytest.raises(RuntimeError, match="POV_PARTICIPATION_REQUIRED"):
            commit_turn_request(sid, payload)

        assert storage._read_turns(root) == []
        pending = storage._read_json(root / "turn_packet.json", {})
        assert pending.get("packet_id") == packet["packet_id"]
