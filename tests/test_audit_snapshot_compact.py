import json
import tempfile
from pathlib import Path

from app import audit_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_audit_snapshot_stays_action_safe_with_large_state_and_memory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "audit_big",
            "title": "Audit Big",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov", "condition": {"note": "X" * 80_000}},
                "current": {
                    "location": "room",
                    "scene": "Y" * 80_000,
                    "present_characters": ["pov", "npc"],
                },
                "characters": {
                    "npc": {"present": True, "location": "room", "huge": "Z" * 80_000}
                },
                "relationships": {"npc": {"trust": 17}},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 45
        meta["last_audit_turn"] = 30
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        for turn in range(31, 46):
            storage._memory_bucket(memory, "npc")["knowledge"].append(
                {"fact_id": f"f{turn}", "learned_turn": turn, "fact": "F" * 5_000}
            )
            storage._memory_bucket(memory, "npc")["experiences"].append(
                {"event_id": f"e{turn}", "turn": turn, "summary": "E" * 5_000}
            )
            storage._memory_bucket(memory, "npc")["dialogue_memory"].append(
                {"topic_id": f"d{turn}", "turn": turn, "summary": "D" * 5_000}
            )
        storage._write_json(root / "memory.json", memory)

        chronology = [
            {
                "event_id": f"c{turn}",
                "turn_number": turn,
                "event": "C" * 5_000,
                "participants": ["pov", "npc"],
            }
            for turn in range(31, 46)
        ]
        storage._write_json(root / "chronology.json", chronology)

        snapshot = audit_runtime.get_audit_snapshot(sid)
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

        assert snapshot["audit_range"] == [31, 45]
        assert snapshot["snapshot_mode"] == "compact_action_safe"
        assert snapshot["response_chars"] <= audit_runtime.AUDIT_RESPONSE_TARGET_CHARS
        assert len(encoded) < 30_000
        assert snapshot["state"]["present_character_ids"] == ["pov", "npc"]
        assert snapshot["saved_counts"]["chronology_total_this_cycle"] == 15
        assert snapshot["saved_this_cycle"]["chronology"]
        assert snapshot["saved_this_cycle"]["memory"]["npc"]["knowledge"]
        assert "X" * 2_000 not in encoded
        assert "Y" * 2_000 not in encoded
        assert "Z" * 2_000 not in encoded
