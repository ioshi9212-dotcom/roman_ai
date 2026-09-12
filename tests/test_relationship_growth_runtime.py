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


def extracted():
    return {"persistence_reviewed": True, "chronology": [], "knowledge_add": [], "experiences_add": [], "dialogue_memory_add": []}


def scene(metrics, turn=1):
    row = f"Эдриан - {metrics}\n" if metrics else ""
    return f"🎭 Test\n\nСцена.\n\nСостояние: нормально\nОтношения:\n{row}\nХод {turn} · цикл {turn}/15"


def test_new_dimension_is_appended_after_initial_schema():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"симпатия": 10, "настороженность": 8}))["session_id"]
        read_packet(sid, "test")
        session_runtime.commit_turn(sid, {"user_input": "test", "scene_output": scene("симпатия 11/+1; настороженность 7/-1; доверие 6/+6"), "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 11, "настороженность": 7, "доверие": 6}


def test_multiple_allowed_dimensions_can_accumulate_without_replacing_old_ones():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"симпатия": 10}))["session_id"]
        read_packet(sid, "one")
        session_runtime.commit_turn(sid, {"user_input": "one", "scene_output": scene("симпатия 10; доверие 4/+4; ревность 3/+3"), "extracted": extracted()})
        read_packet(sid, "two")
        session_runtime.commit_turn(sid, {"user_input": "two", "scene_output": scene("симпатия 10; доверие 5/+1; ревность 3; уважение 7/+7", turn=2), "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"симпатия": 10, "доверие": 5, "ревность": 3, "уважение": 7}


def test_zero_dimensions_may_be_hidden_but_remain_persisted():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"ревность": 0, "доверие": 0}))["session_id"]
        read_packet(sid, "quiet")
        session_runtime.commit_turn(sid, {"user_input": "quiet", "scene_output": scene(""), "extracted": extracted()})
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"ревность": 0, "доверие": 0}


def test_unknown_visible_dimension_is_ignored_without_blocking_valid_saved_changes():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3, "настороженность": 4}))["session_id"]
        read_packet(sid, "conflict")
        session_runtime.commit_turn(
            sid,
            {"user_input": "conflict", "scene_output": scene("доверие 1/-2; настороженность 5/+1; скепсис 6/+6"), "extracted": extracted()},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"] == {"доверие": 1, "настороженность": 5}


def test_unknown_new_dimension_in_canonical_relationship_update_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3}))["session_id"]
        read_packet(sid, "conflict")
        payload = extracted()
        payload["relationship_updates"] = [
            {"character_id": "adrian", "dimensions": [{"label": "скепсис", "value": 6, "delta": 1}]}
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

def test_explicit_existing_delta_out_of_range_is_rejected_instead_of_silently_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3}))["session_id"]
        read_packet(sid, "conflict")
        payload = extracted()
        payload["relationship_updates"] = [
            {"character_id": "adrian", "dimensions": [{"label": "доверие", "value": 7, "delta": 4}]}
        ]
        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                {"user_input": "conflict", "scene_output": scene("доверие 3"), "extracted": payload},
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_DELTA_OUT_OF_RANGE"
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["доверие"] == 3


def test_legacy_footer_out_of_range_delta_stays_non_destructive_and_non_blocking():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3}))["session_id"]
        read_packet(sid, "legacy")
        session_runtime.commit_turn(
            sid,
            {"user_input": "legacy", "scene_output": scene("доверие 7/+4"), "extracted": extracted()},
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["доверие"] == 3


def test_packet_and_rules_agree_relationship_updates_are_canonical():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel({"доверие": 3}))["session_id"]
        packet = read_packet(sid, "quiet")
        assert packet["relationship_policy"]["footer_is_display_only"] is True
        assert "Existing metrics change only through relationship_updates delta" in packet["relationship_policy"]["instruction"]
        assert "единственный canonical-канал" in packet["runtime_rules"]
        assert "Footer только показывает актуальные числа" in packet["runtime_rules"]
