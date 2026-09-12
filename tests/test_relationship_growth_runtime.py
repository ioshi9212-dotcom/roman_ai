import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import session_runtime, storage


ROOT = Path(__file__).resolve().parents[1]


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel(relationships):
    return {
        "novel_id": "relationship-growth",
        "title": "Relationship Growth",
        "novel": {"pov_character": "rina"},
        "characters": [{"character_id": "rina", "name": "Рината", "is_pov": True}, {"character_id": "adrian", "name": "Эдриан"}],
        "starting_state": {"pov": {"character_id": "rina"}, "current": {"location": "room", "present_characters": ["rina", "adrian"]}, "relationships": {"adrian": relationships}},
    }


def read_packet(sid, user_input):
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    text = "".join(storage.get_turn_packet_chunk(sid, manifest["packet_id"], i)["content"] for i in range(manifest["chunk_count"]))
    return json.loads(text)


def extracted(**extra):
    result = {"persistence_reviewed": True, "chronology": [], "knowledge_add": [], "experiences_add": [], "dialogue_memory_add": []}
    result.update(extra)
    return result


def scene(metrics, turn=1):
    row = f"Эдриан - {metrics}\n" if metrics else ""
    return f"🎭 Test\n\nСцена.\n\nСостояние: нормально\nОтношения:\n{row}\nХод {turn} · цикл {turn}/15"


def test_new_dimension_is_appended_after_initial_schema():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"симпатия": 10, "настороженность": 8}))["session_id"]
        read_packet(sid, "test")
        updates = [{
            "character_id": "adrian",
            "reason": "Конкретное взаимодействие усилило симпатию и доверие, но немного снизило настороженность.",
            "change_scale": "ordinary",
            "dimensions": [
                {"label": "симпатия", "value": 11, "delta": 1},
                {"label": "настороженность", "value": 7, "delta": -1},
                {"label": "доверие", "value": 6},
            ],
        }]
        session_runtime.commit_turn(
            sid,
            {"user_input": "test", "scene_output": scene("симпатия 11/+1; настороженность 7/-1; доверие 6"), "extracted": extracted(relationship_updates=updates)},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 11, "настороженность": 7, "доверие": 6}


def test_multiple_allowed_dimensions_can_accumulate_without_replacing_old_ones():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"симпатия": 10}))["session_id"]
        read_packet(sid, "one")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "one",
                "scene_output": scene("симпатия 10; доверие 4; ревность 3"),
                "extracted": extracted(relationship_updates=[{
                    "character_id": "adrian",
                    "reason": "Первый конфликт сформировал доверие и ревность как новые значимые оси.",
                    "change_scale": "ordinary",
                    "dimensions": [
                        {"label": "доверие", "value": 4},
                        {"label": "ревность", "value": 3},
                    ],
                }]),
            },
        )
        read_packet(sid, "two")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "two",
                "scene_output": scene("симпатия 10; доверие 5/+1; ревность 3; уважение 7", turn=2),
                "extracted": extracted(relationship_updates=[{
                    "character_id": "adrian",
                    "reason": "Следующий поступок немного укрепил доверие и впервые дал основание для уважения.",
                    "change_scale": "ordinary",
                    "dimensions": [
                        {"label": "доверие", "value": 5, "delta": 1},
                        {"label": "уважение", "value": 7},
                    ],
                }]),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 10, "доверие": 5, "ревность": 3, "уважение": 7}


def test_zero_dimensions_must_remain_visible_for_present_npc():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"ревность": 0, "доверие": 0}))["session_id"]
        read_packet(sid, "quiet")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(sid, {"user_input": "quiet", "scene_output": scene(""), "extracted": extracted()})
        assert exc.value.detail["code"] == "RELATIONSHIP_FOOTER_INCOMPLETE"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"ревность": 0, "доверие": 0}


def test_new_visible_dimension_cannot_write_canon_without_causal_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3, "настороженность": 4}))["session_id"]
        read_packet(sid, "conflict")
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "conflict", "scene_output": scene("доверие 3; настороженность 4; скепсис 6"), "extracted": extracted()},
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_CHANGE_REASON_REQUIRED"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"доверие": 3, "настороженность": 4}


def test_unknown_new_dimension_in_canonical_relationship_update_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3}))["session_id"]
        read_packet(sid, "conflict")
        payload = extracted()
        payload["relationship_updates"] = [
            {
                "character_id": "adrian",
                "reason": "Конфликт вызвал новую реакцию.",
                "change_scale": "ordinary",
                "dimensions": [{"label": "скепсис", "value": 6, "delta": 1}],
            }
        ]
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "conflict", "scene_output": scene("доверие 3"), "extracted": payload},
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_DIMENSION_UNKNOWN"


def test_custom_gpt_retries_transient_transport_failures_without_advancing_turn():
    text = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "service did not respond" in text
    assert "до 2 раз" in text
    assert "exact payload" in text
    assert len(text) <= 8000

# Relationship growth regression suite intentionally lives outside legacy schema-lock tests.