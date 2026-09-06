import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _make_novel(dormant_count: int):
    characters = [
        {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "POV bio"},
        {"character_id": "present", "name": "Present", "bio": "present bio"},
    ]
    for index in range(dormant_count):
        characters.append(
            {
                "character_id": f"away_{index}",
                "name": f"Away {index}",
                "bio": f"DORMANT_CARD_{index}_" + ("Z" * 25_000),
            }
        )
    return {
        "novel_id": f"size_{dormant_count}",
        "title": "Size Guard",
        "novel": {"pov_character": "pov", "premise": "small fixed canon"},
        "lore": {"rule": "small lore"},
        "characters": characters,
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "location": "room",
                "present_characters": ["pov", "present"],
            },
        },
    }


def _packet_text(session_id: str) -> str:
    manifest = session_runtime.prepare_turn_packet(session_id, "Посмотреть на Present.")
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    assert manifest["chunk_count"] == packet["chunk_count"]
    return "".join(packet["chunks"])


def test_many_large_dormant_dossiers_do_not_bloat_each_turn_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        small_sid = storage.create_session(_make_novel(0))["session_id"]
        large_sid = storage.create_session(_make_novel(24))["session_id"]

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / large_sid / "memory.json", {})
        )
        for index in range(24):
            memory["characters"][f"away_{index}"]["dialogue_memory"] = [
                {
                    "topic_id": f"dormant_{index}",
                    "turn": 1,
                    "summary": f"DORMANT_MEMORY_{index}_" + ("M" * 25_000),
                }
            ]
        storage._write_json(storage.SESSIONS_DIR / large_sid / "memory.json", memory)

        small = _packet_text(small_sid)
        large = _packet_text(large_sid)

        # ~1.2 MB of dormant card+memory payload remains persisted but must not ride every turn.
        assert len(large) - len(small) < 20_000
        assert "DORMANT_CARD_0_" not in large
        assert "DORMANT_CARD_23_" not in large
        assert "DORMANT_MEMORY_0_" not in large
        assert "DORMANT_MEMORY_23_" not in large

        persisted_cards = storage._read_json(storage.SESSIONS_DIR / large_sid / "characters.json", [])
        assert "DORMANT_CARD_23_" in next(
            card["bio"] for card in persisted_cards if card["character_id"] == "away_23"
        )
        persisted_memory = storage._read_json(storage.SESSIONS_DIR / large_sid / "memory.json", {})
        assert "DORMANT_MEMORY_23_" in persisted_memory["characters"]["away_23"]["dialogue_memory"][0]["summary"]
