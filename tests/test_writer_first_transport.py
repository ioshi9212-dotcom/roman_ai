import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.writer_first_runtime import (
    CONTINUITY_WINDOW,
    MAX_ACTIVE_THREADS,
    MAX_HISTORICAL_KNOWLEDGE_CATALOG,
    RECENT_FULL_TURNS,
)


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_context(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    assert manifest["writer_first"] is True
    assert manifest["first_chunk_included"] is True
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(parts))


def novel():
    return {
        "novel_id": "writer-first",
        "title": "Writer First",
        "novel": {"pov_character": "pov", "genre": "romance"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "pov"},
            {"character_id": "npc", "name": "NPC", "bio": "npc"},
            {"character_id": "away", "name": "Away", "bio": "away"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "scene": "talk", "present_characters": ["pov", "npc"], "game_day": 7},
        },
    }


def test_writer_first_packet_has_small_runtime_surface_and_first_chunk_inline():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        turns = []
        for turn in range(1, 31):
            turns.append({
                "turn_number": turn,
                "user_input": f"input {turn}",
                "scene_output": f"scene {turn} " + "S" * 2400,
                "extracted": {
                    "chronology": [{"event_id": f"c{turn}", "event": f"event {turn}"}],
                    "knowledge_add": [], "experiences_add": [], "dialogue_memory_add": [],
                },
            })
        (root / "turns.jsonl").write_text("\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n", encoding="utf-8")
        meta = storage._read_json(root / "meta.json", {})
        meta.update({"turn_number": 30, "last_audit_turn": 30, "audit_required": False})
        storage._write_json(root / "meta.json", meta)

        state = storage._read_json(root / "state.json", {})
        state["threads"] = {
            f"thread-{index}": {"status": "closed" if index < 8 else "active", "priority": index, "notes": "T" * 5000}
            for index in range(30)
        }
        storage._write_json(root / "state.json", state)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        for turn in range(1, 250):
            bucket["knowledge"].append({"fact_id": f"f{turn}", "learned_turn": turn, "fact": f"fact {turn}"})
        storage._write_json(root / "memory.json", memory)

        manifest, context = read_context(sid, "(посмотреть на NPC)")
        assert manifest["chunk_count"] <= 8
        packet = storage._read_json(root / "turn_packet.json", {})
        assert packet["read_chunks"] == list(range(manifest["chunk_count"]))
        assert "writer_contract" not in context
        assert context["runtime_rules"]
        assert context["scene_builder"]
        for removed in (
            "pov_participation_contract", "npc_agency_contract", "relationship_contract",
            "presence_contract", "memory_contract", "continuity_contract",
        ):
            assert removed not in context
        assert len(context["recent_turns"]) == RECENT_FULL_TURNS
        assert len(context["continuity_turns"]) == CONTINUITY_WINDOW - RECENT_FULL_TURNS
        assert len(context["active_threads"]) <= MAX_ACTIVE_THREADS
        assert all(item.get("status") != "closed" for item in context["active_threads"].values())
        assert len(context["character_memory"]["npc"]["historical_knowledge_catalog"]) <= MAX_HISTORICAL_KNOWLEDGE_CATALOG
        assert set(context["runtime_document_paths"]) == {"rules", "scene_builder"}
        assert context["player_input_map"]["stage_directions"] == ["посмотреть на NPC"]


def test_repeated_prepare_reuses_same_packet_and_replays_chunk_zero_for_lost_response_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        first = session_runtime.prepare_turn_packet(sid, "same")
        second = session_runtime.prepare_turn_packet(sid, "same")
        assert first["packet_id"] == second["packet_id"]
        assert first["first_chunk_included"] is True
        assert second["first_chunk_included"] is True
        assert second["content"] == first["content"]
