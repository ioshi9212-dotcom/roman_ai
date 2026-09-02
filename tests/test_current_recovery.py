import json
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage
from app.session_recovery import recover_session_current


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "current-recovery",
        "title": "Current Recovery",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "03.09.2026",
                "time": "13:50",
                "location": "улица",
                "scene": "до мастерской",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def reviewed(**extra):
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }
    value.update(extra)
    return value


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)
    return manifest


def scene(turn: int = 1):
    return f"""🎭 Current Recovery · осень
🕒 День 2 · четверг, 03.09.2026, 14:20 · 📍 мастерская
🌦️ Погода: ясно
⚙️ Сцена: разговор у верстака
✦ Рина
🧥 Одежда, волосы: обычно
◈ Инвентарь: телефон
--------------------------------------------------------

Эдриан остался рядом с Риной у верстака.

Что я могу сделать:
1. Осмотреться.
2. Подойти ближе.
3. Остаться на месте.

Что я могу сказать:
1. Ладно.
2. И что дальше?
3. Понятно.

Что я могу подумать:
1. Здесь тесно.
2. Интересно.
3. Надо запомнить дорогу.

Состояние: спокойно
Отношения:
Эдриан - настороженность 6; симпатия 4

Ход {turn} · цикл {turn}/15"""


def test_recover_current_repairs_pointer_without_creating_turn_or_touching_canon():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "(зайти в мастерскую)")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "(зайти в мастерскую)",
                "scene_output": scene(),
                "extracted": reviewed(
                    state_patch={
                        "current": {
                            "date": "03.09.2026",
                            "time": "14:20",
                            "location": "мастерская",
                            "scene": "разговор у верстака",
                            "present_characters": ["rina", "adrian"],
                        }
                    },
                    chronology=[
                        {
                            "event": "Рина и Эдриан вошли в мастерскую.",
                            "importance": "normal",
                            "participants_present": ["rina", "adrian"],
                        }
                    ],
                ),
            },
        )

        root = storage.SESSIONS_DIR / sid
        meta_before = deepcopy(storage._read_json(root / "meta.json", {}))
        chronology_before = deepcopy(storage._read_json(root / "chronology.json", []))
        memory_before = deepcopy(storage._read_json(root / "memory.json", {}))
        turns_before = deepcopy(storage._read_turns(root))

        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        storage._write_json(root / "state.json", state)

        checkpoint = session_runtime.continue_session(sid)
        assert checkpoint["current_recovery_required"] is True

        repaired = recover_session_current(sid)
        assert repaired["changed"] is True
        assert repaired["turn_number"] == meta_before["turn_number"] == 1
        assert repaired["turn_created"] is False
        assert repaired["canon_mutated"] is False
        assert repaired["current"]["date"] == "03.09.2026"
        assert repaired["current"]["time"] == "14:20"
        assert repaired["current"]["location"] == "мастерская"
        assert repaired["current"]["scene"] == "разговор у верстака"
        assert set(repaired["current"]["present_characters"]) == {"rina", "adrian"}

        meta_after = storage._read_json(root / "meta.json", {})
        assert meta_after["turn_number"] == meta_before["turn_number"]
        assert storage._read_json(root / "chronology.json", []) == chronology_before
        assert storage._read_json(root / "memory.json", {}) == memory_before
        assert storage._read_turns(root) == turns_before

        checkpoint2 = session_runtime.continue_session(sid)
        assert checkpoint2["current_recovery_required"] is False
        assert checkpoint2["current"]["location"] == "мастерская"


def test_prepare_turn_blocks_when_current_pointer_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        storage._write_json(root / "state.json", state)

        with pytest.raises(HTTPException) as exc:
            session_runtime.prepare_turn_packet(sid, "следующий ход")
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "CURRENT_RECOVERY_REQUIRED"


def test_commit_rejects_destructive_current_replacement():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "test")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": scene(),
                    "extracted": reviewed(state_patch={"current": None}),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "CURRENT_STATE_PATCH_INVALID"
        assert storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})["turn_number"] == 0
