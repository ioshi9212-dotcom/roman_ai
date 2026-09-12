import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_all_packet_chunks(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if parts else 0
    for index in range(start, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def extracted(**extra):
    result = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
    }
    result.update(extra)
    return result


def novel(present=True, starting_relationships=None):
    return {
        "novel_id": "relationships",
        "title": "Relationships",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "room", "present_characters": ["rina", "adrian"] if present else ["rina"]},
            "relationships": starting_relationships or {},
        },
    }


def scene(metrics: str, turn=1):
    return f"""🎭 Relationships · осень

Сцена.

Состояние: нормально
Отношения:
Эдриан - {metrics}

Ход {turn} · цикл {turn}/15"""


def test_flat_relationships_migrate_to_old_generator_documents():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}}))["session_id"]
        context = read_all_packet_chunks(sid, "test")
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert "relationship_documents" in state
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["target_character_id"] == "rina"
        assert {(item["label"], item["value"]) for item in relation["dimensions"]} == {("симпатия", 10), ("близость", 5)}
        assert "relationship_schemas" not in state
        assert "writer_contract" not in context
        assert "relationship_contract" not in context
        assert "runtime_documents" not in context
        assert context["runtime_rules"]
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {"симпатия": 10, "близость": 5}


def test_causal_update_persists_while_footer_only_displays():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 10, "близость": 5}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "test",
                "scene_output": scene("симпатия 12/+2; близость 5"),
                "extracted": extracted(relationship_updates=[{
                    "character_id": "adrian",
                    "reason": "Поступок POV заметно усилил симпатию Эдриана.",
                    "change_scale": "ordinary",
                    "dimensions": [{"label": "симпатия", "value": 12, "delta": 2}],
                }]),
            },
        )
        assert result["relationships_persisted_from_footer"] is True
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 12, "близость": 5}


def test_wrong_visible_metric_words_are_rejected_when_saved_metrics_disappear():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 35, "доверие": 18, "влечение": 42}}))["session_id"]
        read_all_packet_chunks(sid, "first")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "first", "scene_output": scene("интерес 70; нежность 55"), "extracted": extracted()},
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_FOOTER_INCOMPLETE"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 35, "доверие": 18, "влечение": 42}


def test_partial_footer_is_rejected_instead_of_hiding_saved_dimensions():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 35, "доверие": 18, "влечение": 42}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene("симпатия 35"), "extracted": extracted()})
        assert exc.value.detail["code"] == "RELATIONSHIP_FOOTER_INCOMPLETE"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 35, "доверие": 18, "влечение": 42}


def test_missing_relation_recovers_from_last_visible_footer():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        with (root / "turns.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"turn_number": 4, "scene_output": scene("симпатия 22; доверие 11", turn=4)}, ensure_ascii=False) + "\n")
        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {}
        state.pop("relationship_documents", None)
        storage._write_json(root / "state.json", state)

        context = read_all_packet_chunks(sid, "reunion")
        repaired = storage._read_json(root / "state.json", {})
        assert repaired["relationships"]["adrian"] == {"симпатия": 22, "доверие": 11}
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {"симпатия": 22, "доверие": 11}


def test_relationship_key_by_character_name_is_canonicalized_to_id():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"Эдриан": {"симпатия": 10}}))["session_id"]
        context = read_all_packet_chunks(sid, "test")
        assert context["relationship_policy"]["authoritative_start_snapshot"]["adrian"]["metrics"] == {"симпатия": 10}
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "Эдриан" not in state["relationships"]
        assert state["relationships"]["adrian"]["симпатия"] == 10


def test_absent_npc_footer_is_ignored_and_cannot_overwrite_relationship():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(present=False, starting_relationships={"adrian": {"симпатия": 10}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene("симпатия 99/+89"), "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 10


def test_existing_metric_absolute_update_without_delta_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 10, "доверие": 5}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        payload = extracted()
        payload["relationship_updates"] = [
            {"character_id": "adrian", "dimensions": [{"label": "симпатия", "value": 99}]}
        ]
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "test", "scene_output": scene("симпатия 10; доверие 5"), "extracted": payload},
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_DELTA_REQUIRED"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 10, "доверие": 5}


def test_leave_transition_with_stale_visible_footer_does_not_block_or_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 10}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        payload = extracted()
        payload["presence_updates"] = [{"character_id": "adrian", "action": "leave"}]
        session_runtime.commit_turn(
            sid,
            {"user_input": "test", "scene_output": scene("симпатия 99/+89"), "extracted": payload},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "adrian" not in state["current"]["present_characters"]
        assert state["relationships"]["adrian"]["симпатия"] == 10


def test_explicit_delta_uses_saved_baseline_not_supplied_absolute_value():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 50, "доверие": 20}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        payload = extracted()
        payload["relationship_updates"] = [
            {
                "character_id": "adrian",
                "reason": "Конкретное действие POV усилило симпатию.",
                "change_scale": "ordinary",
                "dimensions": [{"label": "симпатия", "value": 12, "delta": 2}],
            }
        ]
        session_runtime.commit_turn(
            sid,
            {"user_input": "test", "scene_output": scene("симпатия 52/+2; доверие 20"), "extracted": payload},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 52, "доверие": 20}


def test_absolute_value_without_delta_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 50}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        payload = extracted()
        payload["relationship_updates"] = [
            {"character_id": "adrian", "dimensions": [{"label": "симпатия", "value": 31}]}
        ]
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "test", "scene_output": scene("симпатия 50"), "extracted": payload},
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_DELTA_REQUIRED"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["симпатия"] == 50


def test_footer_delta_does_not_persist_when_npc_leaves_without_causal_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(starting_relationships={"adrian": {"симпатия": 10}}))["session_id"]
        read_all_packet_chunks(sid, "test")
        payload = extracted()
        payload["presence_updates"] = [{"character_id": "adrian", "action": "leave"}]
        session_runtime.commit_turn(
            sid,
            {"user_input": "test", "scene_output": scene("симпатия 11/+1"), "extracted": payload},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "adrian" not in state["current"]["present_characters"]
        assert state["relationships"]["adrian"]["симпатия"] == 10
