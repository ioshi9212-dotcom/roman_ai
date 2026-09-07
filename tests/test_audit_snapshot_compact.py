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


def test_audit_snapshot_keeps_exact_15_turns_without_retransmitting_lifetime_storage():
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
                {"character_id": "pov", "name": "POV", "is_pov": True, "bio": "pov bio"},
                {"character_id": "npc", "name": "NPC", "bio": "npc bio"},
                {"character_id": "away", "name": "Away", "bio": huge_z},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "scene": "current", "present_characters": ["pov", "npc"]},
                "characters": {"npc": {"present": True, "location": "room"}, "away": {"present": False, "huge": huge_z}},
                "relationships": {"npc": {"trust": 17}},
                "relationship_documents": {"npc": {"history": huge_y}, "away": {"history": huge_z}},
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
        for turn in range(1, 31):
            storage._memory_bucket(memory, "npc")["knowledge"].append(
                {"fact_id": f"old-f{turn}", "learned_turn": turn, "fact": huge_x}
            )
            storage._memory_bucket(memory, "npc")["experiences"].append(
                {"event_id": f"old-e{turn}", "turn": turn, "summary": huge_y}
            )
        for turn in range(31, 46):
            storage._memory_bucket(memory, "npc")["knowledge"].append(
                {"fact_id": f"f{turn}", "learned_turn": turn, "fact": f"fact-{turn}"}
            )
            storage._memory_bucket(memory, "npc")["experiences"].append(
                {"event_id": f"e{turn}", "turn": turn, "summary": f"experience-{turn}"}
            )
            storage._memory_bucket(memory, "npc")["dialogue_memory"].append(
                {"topic_id": f"d{turn}", "turn": turn, "summary": f"dialogue-{turn}"}
            )
        storage._memory_bucket(memory, "away")["knowledge"].append(
            {"fact_id": "old-away", "learned_turn": 2, "fact": huge_z}
        )
        storage._write_json(root / "memory.json", memory)

        chronology = []
        for turn in range(1, 31):
            chronology.append(
                {
                    "event_id": f"old-c{turn}",
                    "turn_number": turn,
                    "event": f"old-{turn}",
                    "participants": ["pov", "npc"],
                    "importance": "anchor" if turn <= 28 else "normal",
                }
            )
        for turn in range(31, 46):
            chronology.append(
                {
                    "event_id": f"c{turn}",
                    "turn_number": turn,
                    "event": f"current-{turn}",
                    "participants": ["pov", "npc"],
                }
            )
        storage._write_json(root / "chronology.json", chronology)

        with (root / "turns.jsonl").open("w", encoding="utf-8") as fh:
            for turn in range(31, 46):
                fh.write(
                    json.dumps(
                        {
                            "turn_number": turn,
                            "user_input": f"input-{turn}",
                            "scene_output": f"scene-{turn}-NPC-" + ("S" * 2400),
                            "extracted": {"chronology": [], "knowledge_add": [], "experiences_add": [], "dialogue_memory_add": []},
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        manifest = audit_runtime.get_audit_snapshot(sid)
        assert manifest["audit_range"] == [31, 45]
        assert manifest["chunk_count"] < 20
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
        assert payload["source_reference"]["novel_id"] == "audit_big"
        assert "source_full" not in payload
        assert "runtime_documents_full" not in payload
        assert "state_full" not in payload
        assert "relationships" not in payload["state_audit"]
        assert "relationship_documents" not in payload["state_audit"]
        assert len(payload["audit_turns_full"]) == 15
        assert payload["audit_turns_full"][0]["turn_number"] == 31
        assert payload["audit_turns_full"][-1]["turn_number"] == 45
        assert set(payload["audit_character_ids"]) == {"pov", "npc"}
        assert set(payload["memory_audit"]["characters"]) == {"pov", "npc"}
        assert "away" not in payload["memory_audit"]["characters"]
        npc_memory = payload["memory_audit"]["characters"]["npc"]
        assert len(npc_memory["knowledge"]) == 15
        assert all(item["learned_turn"] >= 31 for item in npc_memory["knowledge"])
        assert npc_memory["persistent_counts"]["knowledge"] == 45
        assert {item["character_id"] for item in payload["character_registry_index"]} == {"pov", "npc", "away"}
        assert {item["character_id"] for item in payload["character_cards_audit"]} == {"pov", "npc"}

        chronology_turns = {item["turn_number"] for item in payload["chronology_audit"]}
        assert set(range(31, 46)).issubset(chronology_turns)
        assert len([turn for turn in chronology_turns if turn < 31]) <= audit_runtime.MAX_AUDIT_ANCHORS + audit_runtime.MAX_AUDIT_MAJOR + audit_runtime.MAX_AUDIT_PRIOR_RELATED

        # Complete data is still untouched in Railway.
        assert storage._read_json(root / "source.json", {}) == novel
        persisted_memory = storage._read_json(root / "memory.json", {})
        assert persisted_memory["characters"]["away"]["knowledge"][0]["fact_id"] == "old-away"
        assert persisted_memory["characters"]["npc"]["knowledge"][0]["fact_id"] == "old-f1"
        assert len(storage._read_json(root / "chronology.json", [])) == 45
        assert huge_x in storage._read_json(root / "source.json", {})["novel"]["questionnaire"]
        assert manifest["total_chars"] == sum(len(chunk) for chunk in chunks)
