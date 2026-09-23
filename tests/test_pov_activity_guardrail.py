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
    assert rule["min_post_input_presence_beats"] == 2
    assert rule["ordinary_dialogue_required_when_natural"] is True
    assert rule["multiple_pov_lines_allowed"] is True
    assert rule["silence_requires_character_or_scene_reason"] is True
    assert rule["do_not_replace_speech_with_gesture"] is True

    text = rule["rule"]
    assert "не камера и не мебель" in text
    assert "минимум дважды" in text
    assert "сам user_input не засчитывай" in text
    assert "POV отвечает словами" in text
    assert "не заменяй естественный ответ" in text
    assert "reserved_for_player" in text
    assert "ветвящееся/необратимое" in rule["reserved_for_player"]


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
        assert signals["version"] == 15
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["pov_activity"]["min_post_input_presence_beats"] == 2
        assert signals["pov_activity"]["ordinary_dialogue_required_when_natural"] is True
        assert signals["pov_activity"]["do_not_replace_speech_with_gesture"] is True
        assert signals["scene_momentum"]["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
        assert len(json.dumps(signals["pov_activity"], ensure_ascii=False)) < 2200
