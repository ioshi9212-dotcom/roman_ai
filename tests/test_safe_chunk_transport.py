"""Check that ChatGPT never receives batched 48k turn-packet responses."""
import json
import tempfile
from inspect import signature
from pathlib import Path

from app import audit_runtime, session_runtime, storage
from app.main import turn_packet_chunk_get


def _session(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()
    return storage.create_session({
        "novel_id": "safe_chunk_response", "title": "Safe Chunk Response",
        "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov"]},
        },
    })["session_id"]


def test_gameplay_action_returns_one_complete_chunk_at_a_time():
    assert signature(turn_packet_chunk_get).parameters["max_chunks"].default == 1
    with tempfile.TemporaryDirectory() as tmp:
        sid = _session(tmp)
        manifest = session_runtime.prepare_turn_packet(sid, "Прочесть контекст без пропусков.")
        assert manifest["chunk_chars_max"] == 16000
        contents = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            result = turn_packet_chunk_get(sid, manifest["packet_id"], index)
            assert result["chunk_index"] == index
            assert isinstance(result["content"], str)
            assert len(result["content"]) <= 16000
            assert "chunks" not in result
            contents.append(result["content"])
        packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert "".join(contents) == "".join(packet["chunks"])
        assert packet["read_chunks"] == list(range(manifest["chunk_count"]))
        assert json.loads("".join(contents))["character_cards"][0]["name"] == "POV"
        batched_request = turn_packet_chunk_get(sid, manifest["packet_id"], 1, max_chunks=2)
        assert "chunks" not in batched_request  # gameplay never returns oversized batches


def test_audit_action_keeps_same_single_chunk_shape():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _session(tmp)
        root = storage.SESSIONS_DIR / sid
        chunks = ["audit-header", "a" * 16000, "b" * 16000, "audit-end"]
        storage._write_json(root / audit_runtime.AUDIT_PACKET_FILE, {
            "audit_id": "audit-safe-size",
            "audit_range": [1, 15],
            "read_chunks": [0],
            "chunks": chunks,
        })
        received = [chunks[0]]
        for index in range(1, len(chunks)):
            result = turn_packet_chunk_get(sid, "audit-safe-size", index)
            assert result["packet_kind"] == "audit"
            assert result["chunk_index"] == index
            assert "chunks" not in result
            assert result["content"] == chunks[index]
            received.append(result["content"])
        assert received == chunks
        assert storage._read_json(root / audit_runtime.AUDIT_PACKET_FILE, {})["read_chunks"] == list(range(len(chunks)))
