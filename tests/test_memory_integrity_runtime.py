import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage
from app.stability_runtime import _merge_state_patch_exact_relationships


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel_fixture():
    return {
        "novel_id": "memory_integrity",
        "title": "Memory Integrity",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "liam", "name": "Лиам"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "room", "present_characters": ["rina"]},
        },
        "lore": {},
    }


def prepare_all(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def extracted(**extra):
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }
    value.update(extra)
    return value


def test_dialogue_without_participants_is_saved_from_asked_by_and_asked_to():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        user_input = "Вспомнить разговор."
        prepare_all(sid, user_input)

        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": "Рина вспоминает разговор.",
                "extracted": extracted(
                    dialogue_memory_add=[
                        {
                            "topic_id": "birthplace",
                            "asked_by": "Лиам",
                            "asked_to": "Рина",
                            "question": "Где ты родилась?",
                            "answer": "На Западе",
                            "status": "answered",
                        }
                    ]
                ),
            },
        )
        assert result["turn_number"] == 1
        rina = storage.get_character_memory(sid, "rina")["dialogue_memory"]
        liam = storage.get_character_memory(sid, "liam")["dialogue_memory"]
        assert rina[0]["topic_id"] == "birthplace"
        assert liam[0]["topic_id"] == "birthplace"
        assert rina[0]["participants"] == ["liam", "rina"]
        assert rina[0]["asked_by"] == "liam"
        assert rina[0]["asked_to"] == "rina"


def test_character_id_only_dialogue_memory_is_not_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        user_input = "Запомнить важную тему."
        prepare_all(sid, user_input)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": "Рина фиксирует для себя важную тему.",
                "extracted": extracted(
                    dialogue_memory_add=[
                        {"character_id": "rina", "topic_id": "private_topic", "summary": "важная тема"}
                    ]
                ),
            },
        )
        saved = storage.get_character_memory(sid, "rina")["dialogue_memory"]
        assert saved[0]["topic_id"] == "private_topic"
        assert saved[0]["participants"] == ["rina"]


def test_unknown_memory_owner_rejects_commit_without_turn_or_ghost_bucket():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        root = storage.SESSIONS_DIR / sid
        user_input = "Сохранить факт."
        prepare_all(sid, user_input)

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": user_input,
                    "scene_output": "Рина что-то узнаёт.",
                    "extracted": extracted(
                        knowledge_add=[{"character_id": "typo_ghost", "fact_id": "f1", "content": "fact"}]
                    ),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "MEMORY_CHARACTER_UNKNOWN"
        assert storage._read_turns(root) == []
        memory = storage._read_json(root / "memory.json", {})
        assert "typo_ghost" not in memory.get("characters", {})


def test_same_turn_character_upsert_can_receive_memory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel_fixture())["session_id"]
        user_input = "Появляется Нокс."
        prepare_all(sid, user_input)
        session_runtime.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": "Нокс появляется, но остаётся за дверью.",
                "extracted": extracted(
                    character_upserts=[{"character_id": "knox", "name": "Нокс", "role": "recurring"}],
                    experiences_add=[{"character_id": "Нокс", "event_id": "heard_rina", "summary": "услышал Рину"}],
                ),
            },
        )
        assert storage.get_character_memory(sid, "knox")["experiences"][0]["event_id"] == "heard_rina"


def test_relationship_snapshot_patch_is_exact_not_deep_merged():
    state = {
        "relationships": {
            "npc": {"trust": 10, "obsolete": 99},
            "ghost": {"trust": 50},
        },
        "relationship_documents": {"ghost": {"owner_character_id": "ghost", "relations": []}},
        "current": {},
    }
    patch = {
        "relationships": {"npc": {"trust": 11}},
        "relationship_documents": {"npc": {"owner_character_id": "npc", "relations": []}},
    }
    merged = _merge_state_patch_exact_relationships(state, patch)
    assert merged["relationships"] == {"npc": {"trust": 11}}
    assert set(merged["relationship_documents"]) == {"npc"}
