import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    chunks = [
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"]
        for index in range(manifest["chunk_count"])
    ]
    return manifest, json.loads("".join(chunks))


def test_turn_packet_only_transports_full_cards_for_scene_or_current_communication():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        huge = "H" * 80_000
        novel = {
            "novel_id": "scene_scope",
            "title": "Scene Scope",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "pov-card"},
                {"character_id": "present", "name": "Present", "bio": "present-card"},
                {"character_id": "messenger", "name": "Messenger", "bio": "messenger-card"},
                {"character_id": "thread_only", "name": "ThreadOnly", "bio": huge},
                {"character_id": "away", "name": "Away", "bio": huge},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "scene": "table", "present_characters": ["pov", "present"]},
                "characters": {
                    "present": {"present": True, "location": "room"},
                    "messenger": {"present": False, "location": "elsewhere"},
                    "thread_only": {"present": False, "location": "elsewhere", "blob": huge},
                    "away": {"present": False, "location": "elsewhere", "blob": huge},
                },
                "threads": {
                    "old_thread": {"participants": ["thread_only"], "notes": huge},
                },
                "relationships": {
                    "present": {"trust": 12},
                    "thread_only": {"trust": 3},
                },
                "relationship_documents": {
                    "present": {"history": huge},
                    "thread_only": {"history": huge},
                    "away": {"history": huge},
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 99
        meta["audit_required"] = False
        meta["handoff_required"] = False
        storage._write_json(root / "meta.json", meta)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        for character_id in ("pov", "present", "messenger", "thread_only", "away"):
            bucket = storage._memory_bucket(memory, character_id)
            for turn in range(1, 90):
                bucket["knowledge"].append(
                    {"fact_id": f"{character_id}-f{turn}", "learned_turn": turn, "fact": f"known-{character_id}-{turn}-" + ("K" * 1200)}
                )
                bucket["experiences"].append(
                    {"event_id": f"{character_id}-e{turn}", "turn": turn, "summary": "E" * 1200}
                )
                bucket["dialogue_memory"].append(
                    {"topic_id": f"{character_id}-d{turn}", "turn": turn, "summary": "D" * 1200}
                )
        storage._write_json(root / "memory.json", memory)

        manifest, context = _read_packet(sid, "(написать Messenger: привет)")

        card_ids = {row["character_id"] for row in context["character_cards"]}
        assert card_ids == {"pov", "present", "messenger"}
        assert "thread_only" not in card_ids
        assert "away" not in card_ids

        assert set(context["character_memory"]) == {"pov", "present", "messenger"}
        assert set(context.get("scene_characters", {})) <= {"pov", "present", "messenger"}
        assert "historical_knowledge_catalog" in context["character_memory"]["present"]
        assert context["character_memory"]["present"]["older_history_available"]["experience_records_not_full"] > 0

        scene_state = context["scene_state"]
        assert "relationships" not in scene_state
        assert "relationship_documents" not in scene_state
        assert "threads" not in scene_state
        assert set(scene_state.get("characters", {})) <= {"pov", "present", "messenger"}

        assert any(row["character_id"] == "thread_only" for row in context["character_registry"])
        assert "old_thread" in context["active_threads"]
        assert len(context["active_threads"]["old_thread"]["notes"]) < 2000
        assert "characters" not in context["starting_state"]
        assert "relationship_documents" not in context["starting_state"]
        assert manifest["chunk_count"] < 25

        # Nothing was removed from persistent storage.
        persisted_state = storage._read_json(root / "state.json", {})
        persisted_memory = storage._read_json(root / "memory.json", {})
        persisted_cards = storage._read_json(root / "characters.json", [])
        assert "relationship_documents" in persisted_state
        assert "thread_only" in persisted_state["relationship_documents"]
        assert len(persisted_memory["characters"]["thread_only"]["knowledge"]) == 89
        assert any(card["character_id"] == "away" and card["bio"] == huge for card in persisted_cards)


def test_thread_membership_alone_does_not_transport_full_dossier():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "thread_scope",
            "title": "Thread Scope",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "present", "name": "Present"},
                {"character_id": "thread_only", "name": "ThreadOnly", "secret": "dormant dossier"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "scene": "conversation", "present_characters": ["pov", "present"]},
                "threads": {"plot": {"participants": ["thread_only"]}},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        _, context = _read_packet(sid, "(посмотреть на Present)")
        assert {row["character_id"] for row in context["character_cards"]} == {"pov", "present"}
        assert "thread_only" not in context["character_memory"]
        assert any(row["character_id"] == "thread_only" for row in context["character_registry"])
