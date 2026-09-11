from pathlib import Path

from app import scene_logic_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_character_knowledge_contract_excludes_author_only_sources():
    rule = scene_logic_runtime._knowledge_causality_rule()

    assert rule["mandatory"] is True
    assert rule["character_knowledge_is_closed_world"] is True

    author_only = " ".join(rule["author_only_not_character_knowledge"]).casefold()
    assert "pov questionnaire" in author_only
    assert "npc questionnaire" in author_only
    assert "foundation" in author_only
    assert "chronology" in author_only
    assert "recent_turns" in author_only
    assert "continuity_turns" in author_only
    assert "character cards" in author_only
    assert "another character's memory" in author_only

    allowed = " ".join(rule["allowed_sources"]).casefold()
    assert "character_memory" in allowed
    assert "directly saw" in allowed
    assert "every premise" in allowed


def test_gpt_instruction_says_chronology_and_questionnaires_are_not_character_knowledge():
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "анкета POV" in instructions
    assert "анкеты NPC" in instructions
    assert "chronology_recent" in instructions
    assert "не личное знание NPC" in instructions
    assert "foundation_pressure" in instructions
    assert "только как авторские сюжетные семена" in instructions
