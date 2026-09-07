import json
import tempfile
from pathlib import Path

from app import storage
from app.character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_offscreen_character_read_stays_bounded_with_large_lifetime_memory_and_keeps_intent_sources():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "offscreen-300",
            "title": "Offscreen 300",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "ren", "name": "Ren", "bio": "full-card-marker"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"present_characters": ["pov"], "game_day": 40},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 300
        storage._write_json(root / "meta.json", meta)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "ren")
        for turn in range(1, 301):
            bucket["knowledge"].append(
                {"fact_id": f"fact-{turn}", "learned_turn": turn, "fact": f"knowledge {turn} " + "K" * 1200}
            )
            bucket["experiences"].append(
                {"event_id": f"exp-{turn}", "turn": turn, "summary": f"experience {turn} " + "E" * 1000}
            )
            bucket["dialogue_memory"].append(
                {"topic_id": f"dialogue-{turn}", "turn": turn, "summary": f"dialogue {turn} " + "D" * 1000}
            )
        storage._write_json(root / "memory.json", memory)

        state = storage._read_json(root / "state.json", {})
        state["npc_intents"] = {
            "ren": [
                {
                    "intent_id": "old-clue-followup",
                    "character_id": "ren",
                    "kind": "investigation",
                    "summary": "Вернуться к старой зацепке",
                    "status": "active",
                    "priority": 90,
                    "created_turn": 25,
                    "created_game_day": 3,
                    "source_fact_ids": ["fact-25"],
                }
            ]
        }
        storage._write_json(root / "state.json", state)

        manifest = prepare_character_bundle_read(sid, "ren")
        assert manifest["first_chunk_included"] is True
        assert manifest["chunk_index"] == 0
        assert manifest["chunk_count"] <= 5
        pieces = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            pieces.append(get_character_bundle_chunk(sid, "ren", manifest["read_id"], index)["content"])
        payload = json.loads("".join(pieces))

        assert payload["working_bundle"] is True
        assert payload["persistent_lifetime_memory_complete"] is True
        assert payload["card"]["bio"] == "full-card-marker"
        assert payload["active_intents"][0]["intent_id"] == "old-clue-followup"
        knowledge_ids = {item.get("fact_id") for item in payload["personal_memory"]["knowledge"]}
        assert "fact-25" in knowledge_ids
        assert "fact-300" in knowledge_ids
        assert len(payload["personal_memory"]["experiences"]) <= 12
        assert len(payload["personal_memory"]["dialogue_memory"]) <= 12
        assert len(payload["personal_memory"]["historical_knowledge_catalog"]) <= 8
        assert payload["personal_memory"]["persistent_counts"]["knowledge"] == 300
