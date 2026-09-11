import json
import tempfile
from pathlib import Path

import pytest

from app import commit_idempotency_runtime, session_runtime, storage
from app.turn_rollback import rollback_last_turn


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel_fixture():
    return {
        "novel_id": "idempotency_test",
        "title": "Idempotency",
        "novel": {"title": "Idempotency"},
        "characters": [
            {"character_id": "pov", "name": "Рина", "is_pov": True},
            {"character_id": "present", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "02.09.2026",
                "time": "23:50",
                "location": "дом",
                "present_characters": ["pov", "present"],
            },
        },
        "lore": {},
    }


def mark_packet_read(root: Path):
    packet = storage._read_json(root / "turn_packet.json", {})
    packet["read_chunks"] = list(range(packet["chunk_count"]))
    storage._write_json(root / "turn_packet.json", packet)


def valid_payload(user_input: str = "Дождаться полуночи."):
    scene = (
        "🎭 Idempotency · осень\n"
        "🕒 День 2 · четверг, 03.09.2026, 00:05 · 📍 кухня\n"
        "🌦️ Погода: ясно\n"
        "⚙️ Сцена: тест\n"
        "----------------------------------------\n"
        "Тестовая сцена.\n\n"
        "Отношения:\n"
        "Эдриан - доверие 10\n"
    )
    return {
        "user_input": user_input,
        "scene_output": scene,
        "extracted": {
            "persistence_reviewed": True,
            "chronology": [{"event": "Наступили следующие сутки."}],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
            "state_patch": {"current": {"present_characters": ["pov", "present"]}},
        },
    }


def test_exact_duplicate_commit_returns_success_without_second_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = valid_payload()

        session_runtime.prepare_turn_packet(sid, payload["user_input"])
        mark_packet_read(root)
        first = session_runtime.commit_turn(sid, payload)
        assert first["turn_number"] == 1
        assert first["already_committed"] is False
        assert len(storage._read_turns(root)) == 1
        assert not (root / "turn_packet.json").exists()

        second = session_runtime.commit_turn(sid, payload)
        assert second["ok"] is True
        assert second["turn_number"] == 1
        assert second["already_committed"] is True
        assert second["idempotent_replay"] is True
        assert len(storage._read_turns(root)) == 1
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1

        saved = storage._read_turns(root)[-1]
        fingerprint = saved["extracted"].get("_commit_request_fingerprint")
        assert fingerprint == commit_idempotency_runtime._request_fingerprint(payload)


def test_changed_payload_after_success_is_not_treated_as_duplicate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = valid_payload()

        session_runtime.prepare_turn_packet(sid, payload["user_input"])
        mark_packet_read(root)
        session_runtime.commit_turn(sid, payload)

        changed = valid_payload()
        changed["scene_output"] = changed["scene_output"].replace("Тестовая сцена.", "Другая сцена.")
        assert commit_idempotency_runtime._duplicate_result(
            sid, commit_idempotency_runtime._request_fingerprint(changed)
        ) is None
        with pytest.raises(RuntimeError) as exc:
            session_runtime.commit_turn(sid, changed)
        assert str(exc.value) == "TURN_PACKET_REQUIRED"
        assert len(storage._read_turns(root)) == 1


def test_duplicate_commit_wins_over_post_commit_audit_gate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = valid_payload("Ход пятнадцать")
        fingerprint = commit_idempotency_runtime._request_fingerprint(payload)

        turns = []
        for number in range(1, 16):
            extracted = {}
            if number == 15:
                extracted["_commit_request_fingerprint"] = fingerprint
            turns.append({
                "turn_number": number,
                "user_input": payload["user_input"] if number == 15 else f"turn-{number}",
                "scene_output": payload["scene_output"] if number == 15 else "scene",
                "extracted": extracted,
            })
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns),
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        result = session_runtime.commit_turn(sid, payload)
        assert result["ok"] is True
        assert result["turn_number"] == 15
        assert result["already_committed"] is True
        assert result["audit_due"] is True
        assert result["audit_range"] == [1, 15]
        assert len(storage._read_turns(root)) == 15


def test_rollback_removes_duplicate_identity_and_allows_same_turn_to_be_prepared_again():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        payload = valid_payload()
        fingerprint = commit_idempotency_runtime._request_fingerprint(payload)

        session_runtime.prepare_turn_packet(sid, payload["user_input"])
        mark_packet_read(root)
        session_runtime.commit_turn(sid, payload)
        assert commit_idempotency_runtime._duplicate_result(sid, fingerprint) is not None

        rolled = rollback_last_turn(sid, expected_turn_number=1, confirm=True)
        assert rolled["turn_number"] == 0
        assert storage._read_turns(root) == []
        assert commit_idempotency_runtime._duplicate_result(sid, fingerprint) is None

        manifest = session_runtime.prepare_turn_packet(sid, payload["user_input"])
        assert manifest["prepared_for_turn"] == 1
