import tempfile
from pathlib import Path

from app import storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_packet_must_be_fully_read_before_commit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "packet_test",
            "title": "Packet Test",
            "version": 1,
            "novel": {"rules": ["test rule"], "pov_character": "rina"},
            "lore": {"secret": "hidden"},
            "characters": [
                {"character_id": "rina", "name": "Рина"},
                {"character_id": "aiden", "name": "Эйден"},
                {"character_id": "liam", "name": "Лиам"},
            ],
        }
        storage.save_novel(novel)
        meta = storage.create_session(novel)
        sid = meta["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["pov"]["character_id"] = "rina"
        state["current"]["present_characters"] = ["aiden"]
        storage._write_json(root / "state.json", state)

        user_input = "Посмотреть на Лиама и спросить, где он был."
        packet = storage.prepare_turn_packet(sid, user_input)
        assert "rina" in packet["relevant_character_ids"]
        assert "aiden" in packet["relevant_character_ids"]
        assert "liam" in packet["relevant_character_ids"]

        try:
            storage.commit_turn(sid, {"user_input": user_input, "scene_output": "scene", "extracted": {}})
            assert False, "commit must fail before chunks are read"
        except RuntimeError as exc:
            assert str(exc) == "TURN_PACKET_INCOMPLETE"

        for index in range(packet["chunk_count"]):
            chunk = storage.get_turn_packet_chunk(sid, packet["packet_id"], index)
        assert chunk["all_chunks_read"] is True

        result = storage.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": "scene",
                "extracted": {
                    "knowledge_add": [
                        {"character_id": "liam", "fact_id": "f1", "content": "Rina asked where he was", "source": "Rina"}
                    ]
                },
            },
        )
        assert result["turn_number"] == 1
        memory = storage.get_character_memory(sid, "liam")
        assert memory["knowledge"][0]["fact_id"] == "f1"
        assert not (root / "turn_packet.json").exists()
