from pathlib import Path

from app import character_chunk_read, scene_logic_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_character_knowledge_contract_excludes_author_only_sources():
    rule = scene_logic_runtime._knowledge_causality_rule()

    assert rule["mandatory"] is True
    assert rule["character_knowledge_is_closed_world"] is True

    author_only = " ".join(rule["author_only_not_character_knowledge"]).casefold()
    assert "questionnaire" in author_only
    assert "foundation" in author_only
    assert "chronology" in author_only
    assert "recent_turns" in author_only
    assert "continuity_turns" in author_only
    assert "another character" in author_only
    assert "unknown_to_self" in author_only
    assert "another character's memory" in author_only
    allowed = " ".join(rule["allowed_sources"]).casefold()
    assert "knowledge_path" in allowed
    assert "turn_knowledge" in allowed
    assert "self_card_path" in allowed
    assert "canon_fill" in allowed


def test_gpt_instruction_keeps_other_author_context_out_but_allows_self_facts():
    instructions = (ROOT / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "Чужие cards" in instructions
    assert "chronology/history" in instructions
    assert "self-known" in instructions
    assert "source_self_paths" in instructions
    assert "canon_fill" in instructions



def test_offscreen_bundle_frontloads_firewall_with_self_card_exception(monkeypatch):
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
    assert "card_is_author_only" not in bundle["knowledge_firewall"]
    assert bundle["knowledge_firewall"]["card_is_author_only_for_other_characters"] is True
    assert bundle["knowledge_firewall"]["self_card_facts_are_speaker_knowledge"] is True
    assert bundle["knowledge_firewall"]["version"] == 11
    assert bundle["knowledge_firewall"]["closed_world"] is True
    assert bundle["knowledge_firewall"]["character_id"] == "silas"
    assert "personal_memory" in bundle
    assert "character_knowledge" in bundle
    assert bundle["character_knowledge"]["path"] == "personal_memory.knowledge"
    assert bundle["character_knowledge"]["fact_authority"] is True
    assert bundle["author_only_recollection_context"]["fact_authority"] is False
    assert bundle["dialogue_frame"]["knowledge_path"] == "personal_memory.knowledge"
    assert bundle["dialogue_frame"]["self_card_path"] == "card"
    assert bundle["dialogue_frame"]["behavior_paths"] == ["card", "relationship_to_pov", "active_intents"]
    assert "self-known card paths" in bundle["instruction"]


def test_knowledge_guard_routes_new_information_through_turn_knowledge():
    rule = scene_logic_runtime._knowledge_causality_rule()
    allowed = " ".join(rule["allowed_sources"]).casefold()

    assert "turn_knowledge" in allowed
    assert rule["source_before_use"] is True
