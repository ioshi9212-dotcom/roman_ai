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
    return manifest, json.loads(text), text


def test_runtime_is_complete_and_chunked():
    manifest = runtime_access.runtime_manifest()
    chunks = [runtime_access.runtime_chunk(i)["content"] for i in range(manifest["chunk_count"])]
    payload = json.loads("".join(chunks))
    assert set(payload["documents"]) == {
        "rules", "scene_builder", "pov_contract", "npc_agency_contract",
        "relationship_contract", "presence_contract", "memory_contract", "continuity_contract",
    }
    assert "Формат обязателен" in payload["documents"]["scene_builder"]
    assert "POV НЕ должен искусственно молчать" in payload["documents"]["pov_contract"]
    assert "не обязаны выбирать психологически правильное" in payload["documents"]["npc_agency_contract"]
    assert "RELATIONSHIP LENS" in payload["documents"]["relationship_contract"]
    assert "NO KNOWLEDGE LAUNDERING" in payload["documents"]["scene_builder"]
    assert "НЕ ЛЕГАЛИЗОВАТЬ УТЕЧКУ" in payload["documents"]["memory_contract"]
    assert manifest["total_chars"] == sum(len(x) for x in chunks)


def test_turn_packet_uses_single_builder_compatible_working_set_and_preserves_storage():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        marker_novel = "NOVEL_CANON_UNIQUE_4f3a"
        marker_lore = "LORE_CANON_UNIQUE_937b"
        marker_away_card = "DORMANT_CARD_UNIQUE_afe8"
        marker_away_memory = "DORMANT_MEMORY_UNIQUE_b019"
        novel = {
            "novel_id": "working_context",
            "title": "Working Context",
            "novel": {"pov_character": "pov", "questionnaire": marker_novel},
            "rules": {"custom": "rule"},
            "lore": {"public": marker_lore},
            "hidden_lore": {"secret": "secret"},
            "world": {"world": "world"},
            "story_direction": {"direction": "direction"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "pov bio"},
                {"character_id": "npc", "name": "NPC", "bio": "npc bio"},
                {"character_id": "away", "name": "Away", "bio": marker_away_card},
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
        memory["characters"]["pov"]["knowledge"] = [{"fact_id": "old", "fact": "known", "learned_turn": 1}]
        memory["characters"]["away"]["dialogue_memory"] = [{"topic_id": "away-topic", "summary": marker_away_memory, "turn": 1}]
        storage._write_json(root / "memory.json", memory)
        chronology = [{"event_id": "e1", "turn_number": 1, "event": "old event", "importance": "anchor"}]
        storage._write_json(root / "chronology.json", chronology)

        manifest, context, raw = read_packet(sid)
        assert manifest["working_context"] is True
        assert context["working_context_contract"]["single_copy_transport"] is True
        assert context["working_context_contract"]["scene_builder_paths_are_canonical"] is True
        assert context["transport_context_paths"] == {
            "state": "scene_state", "cards": "character_cards", "memory": "character_memory",
            "registry": "character_registry", "chronology": "chronology_recent", "starting_state": "starting_state",
        }
        assert context["scene_state"] == storage._read_json(root / "state.json", {})
        assert {x["character_id"] for x in context["character_cards"]} == {"pov", "npc"}
        assert set(context["character_memory"]) == {"pov", "npc"}
        assert "away" in {x["character_id"] for x in context["character_registry"]}
        assert context["scene_characters"]["pov"]["personal_memory_path"] == "character_memory[pov]"
        assert context["knowledge_guard"]["personal_memory_path"] == "character_memory[character_id]"
        assert context["novel"] == novel["novel"]
        assert context["novel_lore"] == novel["lore"]
        assert context["starting_state"] == novel["starting_state"]

        for removed in ("source_full", "state_full", "scene_character_cards", "scene_character_memory", "character_registry_index"):
            assert removed not in context
        for duplicate in ("novel", "novel_lore", "character_cards", "chronology_recent", "recent_turns"):
            assert duplicate not in context["author_context"]
        assert raw.count(marker_novel) == 1
        assert raw.count(marker_lore) == 1
        assert marker_away_card not in raw
        assert marker_away_memory not in raw

        assert storage._read_json(root / "source.json", {}) == novel
        assert marker_away_card in next(x["bio"] for x in storage._read_json(root / "characters.json", []) if x["character_id"] == "away")
        assert marker_away_memory in storage._read_json(root / "memory.json", {})["characters"]["away"]["dialogue_memory"][0]["summary"]
        assert storage._read_json(root / "chronology.json", []) == chronology
        assert "POV thoughts" in context["knowledge_guard"]["instruction"]
        assert "Mere proximity" in context["knowledge_guard"]["instruction"]
        assert "NEVER keep the leak" in context["knowledge_guard"]["instruction"]
