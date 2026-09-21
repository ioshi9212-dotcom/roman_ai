import json
import tempfile
from pathlib import Path

from app import narrative_guardrails_runtime as guardrails
from app import session_runtime, storage


def _setup_temp_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_pov_activity_rule_is_short_and_keeps_only_core_agency_boundary():
    rule = guardrails._pov_activity_rule()

    assert rule["mandatory"] is True
    text = rule["rule"]
    assert "мелочи" in text
    assert "обычные реплики" in text
    assert "Значимый выбор" in text
    assert len(text) < 140


def test_writer_packet_keeps_meaningful_choice_boundary_without_duplicate_pov_essay():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        novel = {
            "novel_id": "pov_activity_packet",
            "title": "POV Activity Packet",
            "novel": {"pov_character": "elena"},
            "characters": [
                {"character_id": "elena", "name": "Елена", "is_pov": True},
                {"character_id": "liam", "name": "Лиам", "role": "major"},
            ],
            "lore": {},
            "starting_state": {
                "current": {"location": "база", "present_characters": ["elena", "liam"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        packet = session_runtime.prepare_turn_packet(sid, "(работать как обычно)")

        chunks = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            chunks.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(chunks))

        signals = context["narrative_guardrails"]
        assert signals["version"] == 13
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["scene_momentum"]["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
        assert len(json.dumps(signals["pov_activity"], ensure_ascii=False)) < 200
