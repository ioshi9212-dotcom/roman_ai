import json
import tempfile
from pathlib import Path

import pytest

from app import audit_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_audit_snapshot_is_lossless_and_chunked_with_large_context():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        huge_x = "X" * 80_000
        huge_y = "Y" * 80_000
        huge_z = "Z" * 80_000
        novel = {
            "novel_id": "audit_big",
            "title": "Audit Big",
            "novel": {"pov_character": "pov", "questionnaire": huge_x},
            "rules": {"custom": huge_y},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": huge_x},
                {"character_id": "npc", "name": "NPC", "bio": huge_y},
            ],
            "starting_state": {
                "pov": {"character_id": "pov", "condition": {"note": huge_x}},
                "current": {
                    "location": "room",
                    "scene": huge_y,
                    "present_characters": ["pov", "npc"],
                },
                "characters": {
                    "npc": {"present": True, "location": "room", "huge": huge_z}
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
            for turn in range(1, 46)
        ]
        storage._write_json(root / "chronology.json", chronology)

        with (root / "turns.jsonl").open("w", encoding="utf-8") as fh:
            for turn in range(31, 46):
                fh.write(
                    json.dumps(
                        {
                            "turn_number": turn,
                            "user_input": f"input-{turn}-" + huge_x,
                            "scene_output": f"scene-{turn}-" + huge_y,
                            "extracted": {"marker": huge_z},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        manifest = audit_runtime.get_audit_snapshot(sid)
        assert manifest["audit_range"] == [31, 45]
        assert manifest["chunk_count"] > 1
        assert len(json.dumps(manifest, ensure_ascii=False)) < 5_000

        with pytest.raises(RuntimeError, match="AUDIT_PACKET_INCOMPLETE"):
            audit_runtime.require_complete_audit_read(sid, 31, 45)

        chunks = [
            audit_runtime.get_audit_snapshot_chunk(sid, manifest["audit_id"], index)["content"]
            for index in range(manifest["chunk_count"])
        ]
        payload = json.loads("".join(chunks))

        audit_runtime.require_complete_audit_read(sid, 31, 45)
        assert payload["audit_range"] == [31, 45]
        assert payload["source_full"] == novel
        assert payload["state_full"] == storage._read_json(root / "state.json", {})
        assert payload["memory_full"] == memory
        assert payload["chronology_full"] == chronology
        assert payload["character_cards_full"] == storage._load_cards(root, novel)
        assert len(payload["audit_turns_full"]) == 15
        assert payload["audit_turns_full"][0]["turn_number"] == 31
        assert payload["audit_turns_full"][-1]["turn_number"] == 45
        assert huge_x in payload["source_full"]["novel"]["questionnaire"]
        assert huge_y in payload["state_full"]["current"]["scene"]
        assert huge_z in payload["state_full"]["characters"]["npc"]["huge"]
        assert payload["runtime_documents_full"]["scene_builder"]
        assert manifest["total_chars"] == sum(len(chunk) for chunk in chunks)
