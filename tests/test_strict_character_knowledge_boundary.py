from pathlib import Path

from app import character_chunk_read, scene_logic_runtime


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



def test_offscreen_bundle_frontloads_firewall_and_card_is_not_knowledge(monkeypatch):
    monkeypatch.setattr(
        character_chunk_read,
        "get_character_bundle",
        lambda session_id, character_id: {
            "card": {"character_id": character_id, "hidden_fact": "author_only"},
            "current_state": {},
            "pov_familiarity": "known",
            "personal_memory": {"knowledge": [], "experiences": [], "dialogue_memory": []},
            "relationship_to_pov": {},
            "active_intents": [],
        },
    )

    bundle = character_chunk_read._participation_bundle("session", "silas")

    assert next(iter(bundle)) == "knowledge_firewall"
    assert bundle["knowledge_firewall"]["card_is_author_only"] is True
    assert bundle["knowledge_firewall"]["character_id"] == "silas"
    assert "CARD is objective author context" in bundle["instruction"]
    assert "never evidence" in bundle["instruction"]


def test_knowledge_guard_allows_causal_npc_to_npc_information_transfer():
    rule = scene_logic_runtime._knowledge_causality_rule()
    allowed = " ".join(rule["allowed_sources"]).casefold()

    assert "npc-to-npc" in allowed
    assert "real in-story channel" in allowed
