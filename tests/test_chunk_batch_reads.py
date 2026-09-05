import tempfile
from pathlib import Path

from app import storage
from app.main import _read_chunk_batch


SAFE_ACTION_BATCH_COUNT = 2
SAFE_ACTION_CONTENT_CHARS = 12_000


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_turn_packet_batch_is_lossless_and_marks_every_chunk_read():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "batch_packet",
            "title": "Batch Packet",
            "novel": {"pov_character": "rina", "large_rule": "R" * 70000},
            "lore": {"canon": "C" * 15000},
            "characters": [
                {"character_id": "rina", "name": "Рина", "is_pov": True},
                {"character_id": "adrian", "name": "Эдриан"},
            ],
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["pov"] = {"character_id": "rina"}
        state["current"] = {"location": "дом", "present_characters": ["rina", "adrian"]}
        storage._write_json(root / "state.json", state)

        packet = storage.prepare_turn_packet(sid, "Посмотреть на Эдриана.")
        saved = storage._read_json(root / "turn_packet.json", {})
        exact_payload = "".join(saved["chunks"])
        assert packet["chunk_count"] > 4
        assert max(len(chunk) for chunk in saved["chunks"]) <= 6000

        parts = []
        next_index = 0
        while next_index is not None:
            batch = _read_chunk_batch(
                storage.get_turn_packet_chunk,
                sid,
                packet["packet_id"],
                next_index,
                SAFE_ACTION_BATCH_COUNT,
            )
            assert len(batch["content"]) <= SAFE_ACTION_CONTENT_CHARS
            parts.append(batch["content"])
            next_index = batch["next_start_index"]

        assert "".join(parts) == exact_payload
        assert "R" * 70000 in exact_payload
        assert "C" * 15000 in exact_payload

        saved_after = storage._read_json(root / "turn_packet.json", {})
        assert saved_after["read_chunks"] == list(range(packet["chunk_count"]))
        assert batch["all_chunks_read"] is True


def test_two_chunk_batch_is_lossless_and_stops_at_end():
    calls = []

    def getter(session_id, read_id, chunk_index):
        calls.append(chunk_index)
        chunks = ["A", "B", "C"]
        if chunk_index >= len(chunks):
            raise IndexError(chunk_index)
        return {
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "content": chunks[chunk_index],
            "all_chunks_read": chunk_index == len(chunks) - 1,
        }

    first = _read_chunk_batch(getter, "s", "p", 0, 2)
    assert first["content"] == "AB"
    assert first["chunks_read"] == [0, 1]
    assert first["next_start_index"] == 2

    second = _read_chunk_batch(getter, "s", "p", 2, 2)
    assert second["content"] == "C"
    assert second["chunks_read"] == [2]
    assert second["next_start_index"] is None
    assert calls == [0, 1, 2]
