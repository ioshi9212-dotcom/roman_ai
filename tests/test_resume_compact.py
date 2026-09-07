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
