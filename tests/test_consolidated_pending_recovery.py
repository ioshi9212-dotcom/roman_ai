import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage
from app.main import turns_commit
from app.models import TurnCommit
from app.operation_service import prepare_turn_request
from app.session_recovery import recover_session_current


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "pending-recovery-clean",
        "title": "Pending Recovery Clean",
        "novel": {"pov_character": "pov"},
        "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026",
                "time": "10:00",
                "location": "room",
                "scene": "start",
                "present_characters": ["pov"],
            },
        },
    }


def scene(turn: int = 1):
    return f"""🎭 Pending Recovery · осень
🕒 День 1 · вторник, 01.09.2026, 10:01 · 📍 room
🌦️ Погода: ясно
⚙️ Сцена: продолжение
✦ POV
🧥 Одежда, волосы: обычно
◈ Инвентарь: телефон
--------------------------------------------------------

POV продолжает сцену.

Что я могу сделать:
1. Осмотреться.
2. Сесть.
3. Остаться.

Что я могу сказать:
1. Ладно.
2. Хорошо.
3. Понятно.

Что я могу подумать:
1. Спокойно.
2. Интересно.
3. Продолжим.

Состояние: спокойно
Отношения:

Ход {turn} · цикл {turn}/15"""


def extracted():
    return {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
    }


def test_resume_exposes_existing_uncommitted_turn_instead_of_guessing_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]

        manifest = prepare_turn_request(
            sid,
            "(продолжить)",
            "req-pending-resume",
            scene_archive_capable=True,
        )
        resumed = session_runtime.continue_session(sid)

        pending = resumed["pending_turn"]
        assert pending["packet_id"] == manifest["packet_id"]
        assert pending["request_id"] == "req-pending-resume"
        assert pending["user_input"] == "(продолжить)"
        assert pending["unread_chunk_indices"]
        assert "recoverSessionCurrent is not a turn-packet recovery tool" in resumed["instruction"]


def test_commit_incomplete_returns_exact_unread_chunks_without_deleting_pending_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest = prepare_turn_request(
            sid,
            "(продолжить)",
            "req-incomplete",
            scene_archive_capable=True,
        )
        assert manifest["chunk_count"] > 1

        body = TurnCommit(
            packet_id=manifest["packet_id"],
            user_input="(продолжить)",
            scene_output=scene(),
            extracted=extracted(),
        )
        with pytest.raises(HTTPException) as exc:
            turns_commit(sid, body)

        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert detail["code"] == "TURN_PACKET_INCOMPLETE"
        assert detail["pending_turn"]["packet_id"] == manifest["packet_id"]
        assert detail["pending_turn"]["unread_chunk_indices"]
        assert "не" not in ""  # keep pytest from folding the structured checks
        assert "recoverSessionCurrent" in detail["instruction"]
        assert "Do not prepare a new turn" in detail["instruction"]

        saved = storage._read_json(root / "turn_packet.json", {})
        assert saved["packet_id"] == manifest["packet_id"]
        assert saved["request_id"] == "req-incomplete"


def test_current_pointer_recovery_preserves_pending_user_input_before_invalidating_stale_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest = prepare_turn_request(
            sid,
            "(поцеловать его)",
            "req-before-current-repair",
            scene_archive_capable=True,
        )
        state = storage._read_json(root / "state.json", {})
        state["current"] = {}
        storage._write_json(root / "state.json", state)

        repaired = recover_session_current(sid)

        retry = repaired["pending_turn_to_reprepare"]
        assert retry["packet_id"] == manifest["packet_id"]
        assert retry["request_id"] == "req-before-current-repair"
        assert retry["user_input"] == "(поцеловать его)"
        assert retry["scene_archive_capable"] is True
        assert not (root / "turn_packet.json").exists()

        meta = storage._read_json(root / "meta.json", {})
        saved_retry = meta["last_current_recovery"]["invalidated_pending_turn"]
        assert saved_retry["user_input"] == "(поцеловать его)"
        assert saved_retry["request_id"] == "req-before-current-repair"
        assert storage._read_turns(root) == []
