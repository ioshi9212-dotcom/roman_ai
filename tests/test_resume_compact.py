import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "resume-compact",
        "title": "Resume Compact",
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "adrian", "name": "Эдриан"},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "07.09.2026",
                "time": "14:49",
                "location": "дом",
                "scene": "гостиная",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def test_resume_does_not_return_heavy_stores_or_drop_canonical_persistent_data():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {
            f"npc_{index}": {"trust": index, "notes": "x" * 2000}
            for index in range(80)
        }
        state["relationship_documents"] = {
            f"npc_{index}": {"history": "y" * 3000}
            for index in range(80)
        }
        storage._write_json(root / "state.json", state)
        expected_trust = {key: value["trust"] for key, value in state["relationships"].items()}
        expected_document_ids = set(state["relationship_documents"])

        resumed = session_runtime.continue_session(sid)

        assert resumed["resume_payload_compact"] is True
        assert "relationships" not in resumed
        assert "relationship_documents" not in resumed
        assert "chronology_context" not in resumed
        assert resumed["resume_payload_counts"]["relationships"] >= 80
        assert resumed["resume_payload_counts"]["relationship_documents"] >= 80
        assert len(json.dumps(resumed, ensure_ascii=False)) < 20000

        stored_after = storage._read_json(root / "state.json", {})
        # Compatibility repair canonicalizes relationship payloads, so arbitrary
        # non-model fields may disappear. Numeric relationship canon and document
        # identities must survive; a real present NPC may also gain a canonical doc.
        for key, trust in expected_trust.items():
            assert stored_after["relationships"][key]["trust"] == trust
        assert expected_document_ids.issubset(set(stored_after["relationship_documents"]))

        manifest = session_runtime.prepare_turn_packet(sid, "(продолжить)")
        assert manifest["chunk_count"] >= 1


def test_resume_returns_exact_last_committed_scene_output_for_new_chat():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        expected_scene = "🎭 Resume Compact · осень\n\nТочный текст последней сцены.\n\nХод 1 · цикл 1/15"
        turn = {
            "turn_number": 1,
            "saved_at": "2026-09-20T12:00:00+00:00",
            "user_input": "(продолжить)",
            "scene_output": expected_scene,
            "extracted": {
                "chronology": [],
                "knowledge_add": [],
                "experiences_add": [],
                "dialogue_memory_add": [],
            },
        }
        (root / "turns.jsonl").write_text(json.dumps(turn, ensure_ascii=False) + "\n", encoding="utf-8")
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 1
        storage._write_json(root / "meta.json", meta)

        resumed = session_runtime.continue_session(sid)

        assert resumed["last_committed_turn"] == {
            "turn_number": 1,
            "scene_output": expected_scene,
        }
        assert "exact latest saved scene" in resumed["instruction"]
        assert len(json.dumps(resumed, ensure_ascii=False)) < 20000


def test_resume_without_committed_turn_returns_null_last_committed_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]

        resumed = session_runtime.continue_session(sid)

        assert resumed["turn_number"] == 0
        assert resumed["last_committed_turn"] is None
