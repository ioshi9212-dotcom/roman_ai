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


def test_pov_activity_rule_keeps_pov_active_without_stealing_meaningful_choice():
    rule = guardrails._pov_activity_rule()

    assert rule["mandatory"] is True
    assert rule["dialogue_when_natural"] is True
    assert rule["visible_pov_in_scene"] is True
    assert rule["no_forced_speech_or_beat_count"] is True
    assert "min_post_input_presence_beats" not in rule

    text = rule["rule"]
    assert "живой участник сцены" in text
    assert "сам отвечает на обычные вопросы" in text
    assert "вне диалога действует" in text
    assert "не требуй фиксированного" in text
    assert "reserved_for_player" in text
    assert "серьёзный риск" in rule["reserved_for_player"]


def test_writer_packet_contains_active_pov_guard_and_meaningful_choice_boundary():
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
        assert signals["version"] == 20
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["pov_activity"]["visible_pov_in_scene"] is True
        assert signals["pov_activity"]["dialogue_when_natural"] is True
        assert signals["pov_activity"]["no_forced_speech_or_beat_count"] is True
        assert signals["scene_momentum"]["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
        assert len(json.dumps(signals["pov_activity"], ensure_ascii=False)) < 2200
