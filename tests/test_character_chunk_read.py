import json
import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage
from app.character_chunk_read import (
    CHARACTER_CHUNK_CHARS,
    CHARACTER_MEMORY_TEXT_CHARS,
    get_character_bundle_chunk,
    prepare_character_bundle_read,
)


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "character_chunk",
        "title": "Character Chunk",
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "present", "name": "Present"},
            {"character_id": "away", "name": "Away", "bio": "DORMANT_CARD_" + "C" * 18000},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov", "present"]},
        },
    }


def test_character_bundle_chunks_preserve_full_card_but_bound_memory_transport():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        full_memory_text = "DORMANT_MEMORY_" + "M" * 22000
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        memory["characters"]["away"]["experiences"] = [
            {"event_id": "huge", "event": full_memory_text}
        ]
        storage._write_json(root / "memory.json", memory)

        manifest = prepare_character_bundle_read(sid, "away")
        assert manifest["first_chunk_included"] is True
        assert manifest["chunk_index"] == 0
        parts = [manifest["content"]]
        assert len(parts[0]) <= CHARACTER_CHUNK_CHARS
        for index in range(1, manifest["chunk_count"]):
            chunk = get_character_bundle_chunk(sid, "away", manifest["read_id"], index)
            assert len(chunk["content"]) <= CHARACTER_CHUNK_CHARS
            parts.append(chunk["content"])

        rebuilt = json.loads("".join(parts))
        assert rebuilt["working_bundle"] is True
        assert rebuilt["persistent_lifetime_memory_complete"] is True
        assert "DORMANT_CARD_" in rebuilt["card"]["bio"]
        assert len(rebuilt["card"]["bio"]) > 18000  # card itself remains complete
        experience = rebuilt["personal_memory"]["experiences"][0]
        assert experience["event_id"] == "huge"
        assert experience["event"].startswith("DORMANT_MEMORY_")
        assert len(experience["event"]) <= CHARACTER_MEMORY_TEXT_CHARS + 80
        assert rebuilt["personal_memory"]["persistent_counts"]["experiences"] == 1
        assert rebuilt["personal_memory"]["oversized_record_text_bounded_in_transport"] is True

        # Transport compaction never mutates the persistent lifetime record.
        stored = storage._read_json(root / "memory.json", {})
        assert stored["characters"]["away"]["experiences"][0]["event"] == full_memory_text


def test_character_read_detects_dossier_change():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = prepare_character_bundle_read(sid, "away")
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        memory["characters"]["away"]["knowledge"] = [{"fact_id": "changed", "fact": "new"}]
        storage._write_json(root / "memory.json", memory)
        with pytest.raises(PermissionError):
            get_character_bundle_chunk(sid, "away", manifest["read_id"], 1 if manifest["chunk_count"] > 1 else 0)


def test_dormant_dossier_stays_out_of_normal_packet_but_is_retrievable():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "Посмотреть на Present.")
        packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        text = "".join(packet["chunks"])
        assert manifest["chunk_count"] == packet["chunk_count"]
        assert "DORMANT_CARD_" not in text
        assert "prepareCharacterBundleRead" in text
        read = prepare_character_bundle_read(sid, "away")
        assert read["first_chunk_included"] is True
        assert read["chunk_count"] >= 2  # huge full card still requires safe chunking
