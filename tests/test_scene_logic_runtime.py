import json
import tempfile
from pathlib import Path

from app import scene_logic_runtime as scene_logic
from app import session_runtime, storage


ROOT = Path(__file__).resolve().parents[1]


def _setup_temp_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()



def test_knowledge_causality_requires_source_before_use_and_forbids_retroactive_patch():
    rule = scene_logic._knowledge_causality_rule()
    assert rule["mandatory"] is True
    assert rule["source_before_use"] is True
    assert rule["no_retroactive_justification"] is True
    assert rule["character_knowledge_is_closed_world"] is True
    allowed = " ".join(rule["allowed_sources"]).casefold()
    author_only = " ".join(rule["author_only_not_character_knowledge"]).casefold()
    assert "character_memory" in allowed
    assert "real in-story channel" in allowed
    assert "inference" in allowed
    assert "questionnaire" in author_only
    assert "chronology" in author_only
    assert "future_guidance" in author_only
    assert "another character's memory" in author_only
    assert "Источник должен существовать до реплики" in rule["rule"]
    assert "Не придумывай источник задним числом" in rule["rule"]

def test_player_input_order_rule_forbids_reordering_segments():
    rule = scene_logic._player_input_order_rule()

    assert rule["mandatory"] is True
    assert rule["left_to_right"] is True
    assert rule["no_reordering"] is True
    assert rule["source_path"] == "player_input_map.ordered_segments"



def test_player_text_cleanup_corrects_errors_without_rewriting_voice():
    rule = scene_logic._player_text_cleanup_rule()
    assert rule["mandatory"] is True
    text = rule["rule"]
    assert "опечатки" in text
    assert "орфографию" in text
    assert "мат" in text
    assert "сленг" in text
    assert "не переписывай" in text
    assert len(text) < 180

def test_final_writer_packet_contains_scene_logic_guardrails():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        novel = {
            "novel_id": "scene_logic_packet",
            "title": "Scene Logic Packet",
            "novel": {"pov_character": "emily"},
            "characters": [
                {"character_id": "emily", "name": "Эмили", "is_pov": True},
                {"character_id": "ren", "name": "Рен", "role": "major"},
            ],
            "lore": {},
            "starting_state": {
                "current": {"location": "кофейня", "present_characters": ["emily", "ren"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        packet = session_runtime.prepare_turn_packet(sid, "Я ему скрины не показывала. (убрать телефон)")

        chunks = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            chunks.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(chunks))

        assert next(iter(context)) == "scene_logic_guardrails"
        guards = context["scene_logic_guardrails"]
        assert guards["version"] == 3
        assert guards["knowledge_causality"]["mandatory"] is True
        assert guards["knowledge_causality"]["source_before_use"] is True
        assert guards["knowledge_causality"]["character_knowledge_is_closed_world"] is True
        assert guards["player_input_order"]["mandatory"] is True
        assert guards["player_input_order"]["no_reordering"] is True
        assert guards["player_text_cleanup"]["mandatory"] is True
        assert packet["scene_logic_guardrails"] is True


def test_runtime_and_custom_gpt_repeat_source_order_and_spelling_policy():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "должен существовать ДО реплики" in rules
    assert "Не придумывай источник задним числом" in rules
    assert "исправляй только явные ошибки" in rules
    assert "ordered_segments" in rules
    assert "слева направо" in rules
    assert "scene_logic_guardrails" in instructions
    assert "ordered_segments" in instructions
    assert "информационную дыру задним числом" in instructions
    assert "очевидную орфографию" in instructions
    assert len(instructions) <= 8000
