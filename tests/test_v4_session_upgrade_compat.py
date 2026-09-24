import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.long_horizon_audit import macro_due


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _legacy_novel():
    return {
        "novel_id": "legacy-live-session",
        "title": "Legacy",
        "version": 4,
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina", "inventory": ["телефон"]},
            "current": {
                "date": "24.09.2026",
                "time": "18:00",
                "location": "квартира",
                "present_characters": ["rina", "adrian"],
            },
            "relationships": {"adrian": {"доверие": 4}},
        },
    }


def test_existing_v4_session_without_new_state_keys_can_continue_after_upgrade():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_legacy_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        # Emulate a session saved before remote/state-item/profile-v5 fields existed.
        state = storage._read_json(root / "state.json", {})
        for key in ("remote_characters", "remote_channels", "scene_items", "unfinished_actions"):
            state["current"].pop(key, None)
        storage._write_json(root / "state.json", state)

        memory = storage._read_json(root / "memory.json", {})
        for bucket in memory.get("characters", {}).values():
            if isinstance(bucket, dict):
                bucket.pop("knowledge_journal", None)
        memory["characters"]["adrian"]["knowledge"] = [
            {"fact_id": "legacy_fact", "fact": "Рина обещала позвонить.", "learned_turn": 1}
        ]
        storage._write_json(root / "memory.json", memory)

        manifest = session_runtime.prepare_turn_packet(sid, "(продолжить разговор)")
        assert manifest.get("simple_profile_runtime") is not True
        first = 1 if manifest.get("first_chunk_included") else 0
        for index in range(first, manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)

        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(продолжить разговор)",
                "scene_output": (
                    "🎭 Legacy · осень\n"
                    "🕒 День 1 · четверг, 24.09.2026, 18:05 · 📍 квартира\n\n"
                    "Рина и Эдриан продолжают разговор.\n\n"
                    "Состояние: нормально\nОтношения:\n\nХод 1"
                ),
                "extracted": {
                    "persistence_reviewed": True,
                    "knowledge_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "npc_intent_updates": [],
                    "story_thread_updates": [],
                    "scene_progressed": True,
                },
            },
        )
        assert result["turn_number"] == 1

        persisted_source = storage._read_json(root / "source.json", {})
        persisted_memory = storage._read_json(root / "memory.json", {})
        persisted_state = storage._read_json(root / "state.json", {})
        assert persisted_source["version"] == 4
        assert persisted_memory["characters"]["adrian"]["knowledge"][0]["fact_id"] == "legacy_fact"
        assert persisted_state["relationships"]["adrian"]["доверие"] == 4
        assert persisted_state["current"]["location"] == "квартира"


def test_v4_session_never_enters_v5_60_turn_macro_gate():
    assert macro_due({"version": 4}, 60) is False
    assert macro_due({"version": 4}, 120) is False
