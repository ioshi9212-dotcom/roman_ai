import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.character_access import get_character_bundle
from app.character_chunk_read import (
    CHARACTER_CHUNK_CHARS,
    get_character_bundle_chunk,
    prepare_character_bundle_read,
)
from app import session_runtime


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


def test_character_bundle_chunks_are_lossless_and_response_safe():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        memory["characters"]["away"]["experiences"] = [
            {"event_id": "huge", "event": "DORMANT_MEMORY_" + "M" * 22000}
        ]
        storage._write_json(root / "memory.json", memory)

        manifest = prepare_character_bundle_read(sid, "away")
        parts = []
        for index in range(manifest["chunk_count"]):
            chunk = get_character_bundle_chunk(sid, "away", manifest["read_id"], index)
            assert len(chunk["content"]) <= CHARACTER_CHUNK_CHARS
            parts.append(chunk["content"])

        rebuilt = json.loads("".join(parts))
        assert rebuilt == get_character_bundle(sid, "away")
        assert "DORMANT_CARD_" in rebuilt["card"]["bio"]
        assert "DORMANT_MEMORY_" in rebuilt["personal_memory"]["experiences"][0]["event"]


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
            get_character_bundle_chunk(sid, "away", manifest["read_id"], 0)


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
        assert read["chunk_count"] > 1
