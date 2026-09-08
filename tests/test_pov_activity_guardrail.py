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


def test_pov_activity_rule_allows_ordinary_dialogue_but_reserves_consequential_choices():
    rule = guardrails._pov_activity_rule()

    assert rule["mandatory"] is True
    assert rule["ordinary_dialogue_expected"] is True
    assert rule["multiple_pov_lines_allowed"] is True

    allowed = " ".join(rule["allowed_without_player_input"])
    reserved = " ".join(rule["reserved_for_player"])
    instruction = rule["instruction"]

    assert "шутка" in allowed
    assert "несколько обычных реплик" in allowed
    assert "секрета" in reserved
    assert "сексуальное согласие" in reserved
    assert "не выключается" in instruction
    assert "несколько раз за один ход" in instruction


def test_writer_packet_contains_mandatory_pov_activity_after_writer_first_rewrite():
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
        packet = session_runtime.prepare_turn_packet(sid, "Пожать плечами")

        chunks = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            chunks.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(chunks))

        signals = context["narrative_guardrails"]
        assert signals["version"] == 2
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["pov_activity"]["ordinary_dialogue_expected"] is True
        assert signals["pov_activity"]["multiple_pov_lines_allowed"] is True
        assert "обычный вопрос" in signals["pov_activity"]["instruction"]
