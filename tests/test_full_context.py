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


def test_turn_packet_preserves_full_storage_but_transmits_only_scene_relevant_dossiers():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        huge = "X" * 18000
        novel = {
            "novel_id": "working_context",
            "title": "Working Context",
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
                "pov": {"character_id": "pov"},
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
        assert manifest["working_context"] is True
        assert context["working_context_contract"]["persistent_storage_is_complete"] is True
        assert context["working_context_contract"]["turn_packet_is_scene_scoped"] is True
        assert context["source_character_cards_omitted_from_transport"] is True
        assert "characters" not in context["source_full"]
        assert context["source_full"]["novel"] == novel["novel"]
        assert context["source_full"]["lore"] == novel["lore"]
        assert context["state_full"] == storage._read_json(root / "state.json", {})

        packet_ids = {x["character_id"] for x in context["scene_character_cards"]}
        assert packet_ids == {"pov", "npc"}
        assert set(context["scene_character_memory"]["characters"]) == {"pov", "npc"}
        assert "away" not in context["scene_character_memory"]["characters"]
        assert "away" in {x["character_id"] for x in context["character_registry_index"]}
        assert "personal_memory" not in context["scene_characters"]["pov"]
        assert context["scene_characters"]["pov"]["personal_memory_path"] == "scene_character_memory.characters[pov]"

        persisted_source = storage._read_json(root / "source.json", {})
        assert persisted_source == novel
        persisted_cards = storage._read_json(root / "characters.json", [])
        assert {x["character_id"] for x in persisted_cards} == {"pov", "npc", "away"}
        persisted_memory = storage._read_json(root / "memory.json", {})
        assert persisted_memory["characters"]["away"]["dialogue_memory"][0]["topic_id"] == "away-topic"
        assert storage._read_json(root / "chronology.json", []) == chronology

        assert set(context["present_character_ids_at_turn_start"]) == {"pov", "npc"}
        assert context["runtime_documents"]["rules"]
        assert context["runtime_documents"]["scene_builder"]
        assert context["runtime_documents"]["pov_contract"]
        assert context["runtime_documents"]["npc_agency_contract"]
        assert context["runtime_documents"]["relationship_contract"]
        assert context["pov_participation_contract"] == context["runtime_documents"]["pov_contract"]
        assert "active participant" in context["pov_participation_instruction"]
        assert context["npc_agency_contract"] == context["runtime_documents"]["npc_agency_contract"]
        assert "NOT from universal therapy" in context["npc_agency_instruction"]
        assert context["knowledge_guard"]["mandatory"] is True
        assert context["knowledge_guard"]["personal_memory_path"] == "scene_character_memory.characters[character_id]"
        assert "POV thoughts" in context["knowledge_guard"]["instruction"]
        assert "Mere proximity" in context["knowledge_guard"]["instruction"]
        assert "NEVER keep the leak" in context["knowledge_guard"]["instruction"]
