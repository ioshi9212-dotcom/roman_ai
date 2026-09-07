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
    parts = [manifest["content"]]
    index = manifest.get("next_chunk_index")
    while index is not None:
        chunk = storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)
        parts.append(chunk["content"])
        index = None if chunk.get("all_chunks_read") else index + 1
    text = "".join(parts)
    return manifest, json.loads(text), text


def test_runtime_is_only_rules_and_scene_builder_and_stays_small():
    manifest = runtime_access.runtime_manifest()
    chunks = [runtime_access.runtime_chunk(i)["content"] for i in range(manifest["chunk_count"])]
    payload = json.loads("".join(chunks))
    assert set(payload["documents"]) == {"rules", "scene_builder"}
    rules = payload["documents"]["rules"]
    builder = payload["documents"]["scene_builder"]
    assert "POV — живой участник" in rules
    assert "future_guidance" in rules
    assert "NPC действуют сами" in rules
    assert "Форма обязательна" in builder
    assert "ИСТОРИЯ НЕ ПРИДУМЫВАЕТСЯ ЗАДНИМ ЧИСЛОМ" in builder
    assert manifest["total_chars"] == sum(len(x) for x in chunks)
    assert manifest["total_chars"] < 30000


def test_turn_packet_separates_history_memory_future_guidance_and_preserves_storage():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        marker_novel = "NOVEL_CANON_UNIQUE_4f3a"
        marker_lore = "LORE_CANON_UNIQUE_937b"
        marker_away_card = "DORMANT_CARD_UNIQUE_afe8"
        marker_away_memory = "DORMANT_MEMORY_UNIQUE_b019"
        novel = {
            "novel_id": "working_context", "title": "Working Context",
            "novel": {"pov_character": "pov", "questionnaire": marker_novel},
            "rules": {"custom": "rule"}, "lore": {"public": marker_lore},
            "hidden_lore": {"secret": "secret"}, "world": {"world": "world"},
            "story_direction": {"direction": "future relationship beat"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "pov bio"},
                {"character_id": "npc", "name": "NPC", "bio": "npc bio"},
                {"character_id": "away", "name": "Away", "bio": marker_away_card},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "scene": "start", "present_characters": ["pov", "npc"]},
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
        persistent_state_before = storage._read_json(root / "state.json", {})

        manifest, context, raw = read_packet(sid)
        assert manifest["working_context"] is True
        assert manifest["writer_first"] is True
        assert context["working_context_contract"]["npc_intents_are_persistent"] is True
        assert context["working_context_contract"]["future_guidance_is_not_history"] is True

        scene_state = context["scene_state"]
        assert scene_state["current"]["location"] == "room"
        assert "relationships" not in scene_state
        assert "relationship_documents" not in scene_state
        assert "threads" not in scene_state
        assert "away" not in scene_state.get("characters", {})

        assert {x["character_id"] for x in context["character_cards"]} == {"pov", "npc"}
        assert set(context["character_memory"]) == {"pov", "npc"}
        assert "away" in {x["character_id"] for x in context["character_registry"]}
        assert context["scene_characters"]["pov"]["personal_memory_path"] == "character_memory[pov]"
        assert context["future_guidance"]["story_direction"]["direction"] == "future relationship beat"
        assert context["future_guidance"]["status"] == "future_only_not_history"
        assert "story_direction" not in context

        assert context["relationship_policy"]["authoritative_start_snapshot"]["npc"]["metrics"] == {"trust": 17}
        assert context["runtime_rules"]
        assert context["scene_builder"]
        assert "writer_contract" not in context
        assert context["runtime_document_paths"] == {"rules": "runtime_rules", "scene_builder": "scene_builder"}
        for removed_contract in (
            "pov_participation_contract", "npc_agency_contract", "relationship_contract",
            "presence_contract", "memory_contract", "continuity_contract", "knowledge_guard",
        ):
            assert removed_contract not in context

        assert context["player_input_map"]["spoken_segments"] == ["test"]
        assert raw.count(marker_novel) == 1
        assert raw.count(marker_lore) == 1
        assert marker_away_card not in raw
        assert marker_away_memory not in raw

        assert storage._read_json(root / "source.json", {}) == novel
        persistent_state_after = storage._read_json(root / "state.json", {})
        assert persistent_state_after["relationships"] == persistent_state_before["relationships"]
        assert marker_away_card in next(x["bio"] for x in storage._read_json(root / "characters.json", []) if x["character_id"] == "away")
        assert marker_away_memory in storage._read_json(root / "memory.json", {})["characters"]["away"]["dialogue_memory"][0]["summary"]
        assert storage._read_json(root / "chronology.json", []) == chronology
