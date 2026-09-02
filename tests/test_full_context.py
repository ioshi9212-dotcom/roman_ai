import json
import tempfile
from pathlib import Path

from app import runtime_access, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_packet(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "test")
    text = "".join(
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], i)["content"]
        for i in range(manifest["chunk_count"])
    )
    return manifest, json.loads(text)


def test_runtime_is_complete_and_chunked():
    manifest = runtime_access.runtime_manifest()
    chunks = [runtime_access.runtime_chunk(i)["content"] for i in range(manifest["chunk_count"])]
    payload = json.loads("".join(chunks))
    assert set(payload["documents"]) == {
        "rules",
        "scene_builder",
        "pov_contract",
        "npc_agency_contract",
        "relationship_contract",
        "presence_contract",
        "memory_contract",
        "continuity_contract",
    }
    assert "Формат обязателен" in payload["documents"]["scene_builder"]
    assert "POV НЕ должен искусственно молчать" in payload["documents"]["pov_contract"]
    assert "не обязаны выбирать психологически правильное" in payload["documents"]["npc_agency_contract"]
    assert "он хотел взять её за руку, но не стал" in payload["documents"]["npc_agency_contract"]
    assert "RELATIONSHIP LENS" in payload["documents"]["relationship_contract"]
    assert "Для КАЖДОГО присутствующего NPC отдельная строка ОБЯЗАТЕЛЬНА" in payload["documents"]["relationship_contract"]
    assert "PRESENCE SWEEP" in payload["documents"]["presence_contract"]
    assert "СМЕНА ФОКУСА НЕ РАВНА ИСЧЕЗНОВЕНИЮ" in payload["documents"]["presence_contract"]
    assert "NO KNOWLEDGE LAUNDERING" in payload["documents"]["scene_builder"]
    assert "НЕ ЛЕГАЛИЗОВАТЬ УТЕЧКУ" in payload["documents"]["memory_contract"]
    assert manifest["total_chars"] == sum(len(x) for x in chunks)


def test_turn_packet_contains_full_source_state_cards_memory_and_chronology_without_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        huge = "X" * 18000
        novel = {
            "novel_id": "full_context",
            "title": "Full Context",
            "novel": {"pov_character": "pov", "questionnaire": huge},
            "rules": {"custom": huge},
            "lore": {"public": huge},
            "hidden_lore": {"secret": huge},
            "world": {"world": huge},
            "story_direction": {"direction": huge},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": huge},
                {"character_id": "npc", "name": "NPC", "bio": huge},
                {"character_id": "away", "name": "Away", "bio": huge},
            ],
            "starting_state": {
                "current": {"location": "room", "present_characters": ["pov", "npc"]},
                "relationships": {"npc": {"trust": 17}},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        memory["characters"]["pov"]["knowledge"] = [{"fact_id": "old", "fact": huge, "learned_turn": 1}]
        memory["characters"]["away"]["dialogue_memory"] = [{"topic_id": "away-topic", "summary": huge, "turn": 1}]
        storage._write_json(root / "memory.json", memory)

        chronology = [
            {"event_id": f"e{i}", "turn_number": i, "event": f"event-{i}-" + huge[:200], "importance": "normal"}
            for i in range(1, 81)
        ]
        storage._write_json(root / "chronology.json", chronology)

        manifest, context = read_packet(sid)
        assert manifest["chunk_count"] > 1
        assert context["full_context_contract"]["no_truncation"] is True
        assert context["full_context_contract"]["author_truth_is_quarantined_from_character_knowledge"] is True
        assert context["source_full"] == novel
        assert context["state_full"] == storage._read_json(root / "state.json", {})
        assert context["memory_full"] == memory
        assert context["chronology_full"] == chronology
        assert len(context["chronology_full"]) == 80
        assert context["chronology_full"][0]["event_id"] == "e1"
        assert context["chronology_full"][-1]["event_id"] == "e80"
        assert {x["character_id"] for x in context["all_character_cards"]} == {"pov", "npc", "away"}
        assert {x["character_id"] for x in context["present_character_cards"]} == {"pov", "npc"}
        assert set(context["present_character_ids_at_turn_start"]) == {"pov", "npc"}
        assert context["runtime_documents"]["rules"]
        assert context["runtime_documents"]["scene_builder"]
        assert context["runtime_documents"]["pov_contract"]
        assert context["runtime_documents"]["npc_agency_contract"]
        assert context["runtime_documents"]["relationship_contract"]
        assert context["runtime_documents"]["presence_contract"]
        assert context["runtime_documents"]["memory_contract"]
        assert context["runtime_documents"]["continuity_contract"]
        assert context["pov_participation_contract"] == context["runtime_documents"]["pov_contract"]
        assert "active participant" in context["pov_participation_instruction"]
        assert context["npc_agency_contract"] == context["runtime_documents"]["npc_agency_contract"]
        assert "NOT from universal therapy" in context["npc_agency_instruction"]
        assert "Do not automatically soften" in context["npc_agency_instruction"]
        assert context["knowledge_guard"]["mandatory"] is True
        assert context["knowledge_guard"]["personal_memory_path"] == "memory_full.characters[character_id]"
        assert "Mere proximity" in context["knowledge_guard"]["instruction"]
        assert "NEVER keep the leak" in context["knowledge_guard"]["instruction"]
