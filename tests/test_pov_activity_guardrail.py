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


def test_pov_activity_automates_minutiae_and_requires_natural_ordinary_dialogue():
    rule = guardrails._pov_activity_rule()

    assert rule["mandatory"] is True
    assert rule["ordinary_dialogue_expected"] is True
    assert rule["ordinary_dialogue_required_when_natural"] is True
    assert rule["multiple_pov_lines_allowed"] is True
    assert rule["silence_requires_character_or_scene_reason"] is True

    automatic = " ".join(rule["automatic_without_player_input"])
    anti_silence = " ".join(rule["do_not_silence_pov"])
    reserved = " ".join(rule["reserved_for_player"])
    checks = " ".join(rule["pre_commit_check"])
    instruction = rule["instruction"]

    assert "проверить телефон" in automatic
    assert "продолжение уже выбранной работы" in automatic
    assert "бытовой или нейтральный ответ" in automatic
    assert "несколько обычных реплик" in automatic
    assert "не заменяй естественный словесный ответ" in anti_silence
    assert "говорят только NPC" in anti_silence
    assert "значимое согласие или отказ" in reserved
    assert "сексуальное согласие" in reserved
    assert "раскрытие секрета" in reserved
    assert "не замолчал искусственно" in checks
    assert "POV НЕ должен искусственно молчать" in instruction
    assert "несколько обычных обменов репликами" in instruction
    assert "Молчание допустимо" in instruction


def test_writer_packet_contains_active_pov_dialogue_and_meaningful_choice_boundary():
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
        assert signals["version"] == 9
        assert signals["pov_activity"]["mandatory"] is True
        assert signals["pov_activity"]["ordinary_dialogue_required_when_natural"] is True
        assert signals["pov_activity"]["silence_requires_character_or_scene_reason"] is True
        assert signals["scene_momentum"]["player_input_scope"]["boundary"] == "next_meaningful_pov_choice"
