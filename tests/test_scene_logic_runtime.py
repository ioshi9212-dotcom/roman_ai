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
    assert rule["scene_order_is_causal"] is True
    assert rule["no_retroactive_justification"] is True
    forbidden = " ".join(rule["forbidden"])
    assert "inventing the missing source afterwards" in forbidden
    assert "forgotten detail after the fact" in forbidden
    assert "before the line" in rule["missing_source_behavior"]
    assert "Cause/information must precede reaction/conclusion" in rule["pre_commit_check"]


def test_player_text_cleanup_corrects_errors_without_rewriting_voice():
    rule = scene_logic._player_text_cleanup_rule()

    assert rule["mandatory"] is True
    corrected = " ".join(rule["correct_in_rendered_scene"])
    preserve = " ".join(rule["preserve"])
    do_not = " ".join(rule["do_not"])
    assert "орфографические ошибки" in corrected
    assert "опечатки" in corrected
    assert "мат, сленг" in preserve
    assert "не цензурь" in do_not
    assert "не заменяй слова синонимами" in do_not


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

        guards = context["scene_logic_guardrails"]
        assert guards["version"] == 1
        assert guards["knowledge_causality"]["mandatory"] is True
        assert guards["knowledge_causality"]["source_before_use"] is True
        assert guards["player_text_cleanup"]["mandatory"] is True
        assert packet["scene_logic_guardrails"] is True


def test_runtime_and_custom_gpt_repeat_source_order_and_spelling_policy():
    rules = (ROOT / "runtime" / "rules.md").read_text(encoding="utf-8")
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "должны существовать ДО реплики" in rules
    assert "не придумывай задним числом" in rules
    assert "орфографические ошибки" in rules
    assert "scene_logic_guardrails" in instructions
    assert "информационную дыру задним числом" in instructions
    assert "очевидную орфографию" in instructions
    assert len(instructions) <= 8000
