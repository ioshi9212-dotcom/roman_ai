import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import audit_runtime, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


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


def read_turn_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    text = "".join(
        storage.get_turn_packet_chunk(
            session_id, manifest["packet_id"], index
        )["content"]
        for index in range(manifest["chunk_count"])
    )
    return json.loads(text)


def relationship_novel(starting_relationships=None):
    return {
        "novel_id": "runtime-fixes",
        "title": "Runtime fixes",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "location": "room",
                "present_characters": ["rina", "adrian"],
            },
            "relationships": starting_relationships or {},
        },
    }


def test_turn_packet_uses_one_relationship_model():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            relationship_novel({"adrian": {"симпатия": 10}})
        )["session_id"]

        packet = read_turn_packet(sid, "test")

        policy = packet["relationship_policy"]
        assert policy["source_of_truth"] == "relationship_lens + relationship_contract"
        assert "metric_names_locked" not in policy
        assert policy["authoritative_start_snapshot"]["adrian"]["metrics"] == {
            "симпатия": 10
        }
        assert "relationship_updates" in packet["persistence_contract"]


def test_new_character_upsert_can_persist_first_relationship_same_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = relationship_novel()
        novel["characters"] = [novel["characters"][0]]
        novel["starting_state"]["current"]["present_characters"] = ["rina"]
        sid = storage.create_session(novel)["session_id"]

        read_turn_packet(sid, "(войти в кофейню)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(войти в кофейню)",
                "scene_output": """🎭 Runtime fixes

Мара представилась.

Состояние: спокойно
Отношения:
Мара - симпатия 12; настороженность 8

Ход 1 · цикл 1/15""",
                "extracted": reviewed(
                    character_upserts=[
                        {
                            "character_id": "mara",
                            "name": "Мара",
                            "role": "бариста",
                        }
                    ],
                    state_patch={
                        "current": {
                            "present_characters": ["rina", "mara"],
                        }
                    },
                ),
            },
        )

        assert result["relationship_runtime_fix"] == 1
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["mara"] == {
            "симпатия": 12,
            "настороженность": 8,
        }


def test_departed_npc_relationship_persists_without_visible_footer_line():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            relationship_novel({"adrian": {"симпатия": 10, "доверие": 5}})
        )["session_id"]

        read_turn_packet(sid, "(Эдриан уходит)")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "(Эдриан уходит)",
                "scene_output": """🎭 Runtime fixes

Эдриан ушёл.

Состояние: одна
Отношения:

Ход 1 · цикл 1/15""",
                "extracted": reviewed(
                    state_patch={
                        "current": {
                            "present_characters": ["rina"],
                        }
                    },
                    relationship_updates=[
                        {
                            "character_id": "adrian",
                            "dimensions": [
                                {"label": "симпатия", "value": 11, "delta": 1},
                                {"label": "доверие", "value": 3, "delta": -2},
                            ],
                        }
                    ],
                ),
            },
        )

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {
            "симпатия": 11,
            "доверие": 3,
        }
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["last_changed_turn"] == 1


def test_absent_npc_in_visible_footer_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            relationship_novel({"adrian": {"симпатия": 10}})
        )["session_id"]

        read_turn_packet(sid, "(Эдриан уходит)")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "(Эдриан уходит)",
                    "scene_output": """🎭 Runtime fixes

Состояние: одна
Отношения:
Эдриан - симпатия 11/+1

Ход 1 · цикл 1/15""",
                    "extracted": reviewed(
                        state_patch={
                            "current": {
                                "present_characters": ["rina"],
                            }
                        }
                    ),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_FOOTER_ABSENT_NPC"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 10


def test_bad_relationship_delta_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(
            relationship_novel({"adrian": {"симпатия": 10}})
        )["session_id"]

        read_turn_packet(sid, "test")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "user_input": "test",
                    "scene_output": """🎭 Runtime fixes

Состояние: вместе
Отношения:
Эдриан - симпатия 15/+2

Ход 1 · цикл 1/15""",
                    "extracted": reviewed(),
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_ARITHMETIC_MISMATCH"


def test_audit_repairs_keep_original_turns_and_generate_ids():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["last_audit_turn"] = 0
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        manifest = audit_runtime.get_audit_snapshot(sid)
        payload = json.loads(
            "".join(
                audit_runtime.get_audit_snapshot_chunk(
                    sid, manifest["audit_id"], index
                )["content"]
                for index in range(manifest["chunk_count"])
            )
        )
        assert payload["audit_repair_policy"]["mandatory_original_turn"] is True

        session_runtime.commit_audit(
            sid,
            {
                "start_turn": 1,
                "end_turn": 15,
                "repairs": {
                    "chronology_add": [
                        {
                            "turn_number": 4,
                            "event": "Эдриан передал Ринате ключ.",
                            "participants_present": ["rina", "adrian"],
                        }
                    ],
                    "knowledge_add": [
                        {
                            "character_id": "adrian",
                            "source_turn": 6,
                            "content": "Рината сохранила ключ.",
                        }
                    ],
                },
                "notes": [],
            },
        )

        chronology = storage._read_json(root / "chronology.json", [])
        assert chronology[-1]["turn_number"] == 4
        assert chronology[-1]["event_id"].startswith("audit_chrono_t4_")

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        knowledge = memory["characters"]["adrian"]["knowledge"]
        assert knowledge[-1]["learned_turn"] == 6
        assert knowledge[-1]["fact_id"].startswith("audit_fact_t6_")


def test_invalid_novel_id_cannot_escape_library_directory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        with pytest.raises(HTTPException) as exc:
            storage.save_novel(
                {
                    "novel_id": "../outside",
                    "title": "bad",
                    "novel": {},
                    "characters": [],
                    "lore": {},
                }
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "INVALID_NOVEL_ID"
