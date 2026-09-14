import json
import tempfile
from pathlib import Path

import pytest

from app import session_runtime, storage
from app.scene_compaction_runtime import (
    SCENE_MEMORY_FILE,
    active_memory_records,
    apply_audit_compactions,
    covered_turns,
    load_scene_history,
)


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "scene-compaction",
        "title": "Scene Compaction",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "npc", "name": "NPC"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "location": "room",
                "present_characters": ["pov", "npc"],
            },
        },
    }


def one_scene(start_turn: int, end_turn: int, *, scene_id=None, status="closed", suffix=""):
    row = {
        "start_turn": start_turn,
        "end_turn": end_turn,
        "summary": (
            f"В комнате на ходах {start_turn}–{end_turn} POV последовательно начал разговор с NPC, "
            f"развил его через важные действия и реплики, сохранил все принятые решения и к концу диапазона "
            f"довёл текущий эпизод до зафиксированной точки продолжения{suffix}."
        ),
        "participants": ["pov", "npc"],
        "location": "room",
        "status": status,
    }
    if scene_id:
        row["scene_id"] = scene_id
    return row


def test_fifteen_turn_scene_becomes_one_summary_without_deleting_raw_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].extend(
            [
                {"fact_id": "f1", "character_id": "npc", "learned_turn": 2, "fact": "POV боится воды."},
                {"fact_id": "f2", "character_id": "npc", "learned_turn": 5, "fact": "POV не умеет плавать."},
                {"fact_id": "f3", "character_id": "npc", "learned_turn": 9, "fact": "В воде POV начинает паниковать."},
            ]
        )
        chronology = [
            {
                "event_id": f"e{turn}",
                "turn_number": turn,
                "event": f"Подробное событие хода {turn}",
                "importance": "anchor" if turn in {4, 10} else "major",
            }
            for turn in range(1, 16)
        ]
        repairs = {
            "scene_compactions": [one_scene(1, 15)],
            "memory_compactions": [
                {
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["f1", "f2", "f3"],
                    "summary": "NPC знает, что POV не умеет плавать и в воде испытывает панический страх.",
                }
            ],
        }

        compacted_memory, compacted_chronology, store, resolved = apply_audit_compactions(
            root,
            repairs,
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=chronology,
        )
        storage._write_json(root / SCENE_MEMORY_FILE, store)

        assert len(resolved) == 1
        assert len(store["scenes"]) == 1
        scene = store["scenes"][0]
        assert scene["start_turn"] == 1
        assert scene["end_turn"] == 15
        assert scene["source_ranges"] == [[1, 15]]
        assert len(scene["summary"]) >= 60
        assert covered_turns(root) == set(range(1, 16))

        persisted_knowledge = compacted_memory["characters"]["npc"]["knowledge"]
        assert len(persisted_knowledge) == 4
        raw = {item["fact_id"]: item for item in persisted_knowledge if item["fact_id"] in {"f1", "f2", "f3"}}
        assert set(raw) == {"f1", "f2", "f3"}
        assert all(item.get("raw_evidence_preserved") is True for item in raw.values())
        assert len({item.get("superseded_by") for item in raw.values()}) == 1

        active = active_memory_records(persisted_knowledge)
        assert len(active) == 1
        assert active[0]["canonical_compaction"] is True
        assert active[0]["merged_from"] == ["f1", "f2", "f3"]
        assert set(active[0]["source_turns"]) == {2, 5, 9}
        assert "не умеет плавать" in active[0]["fact"]
        assert "панический страх" in active[0]["fact"]

        assert len(compacted_chronology) == 15
        assert all(item.get("compacted_scene_id") == scene["scene_id"] for item in compacted_chronology)
        assert all(item["event"].startswith("Подробное событие") for item in compacted_chronology)


def test_same_long_scene_reuses_scene_id_across_two_audits():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))

        memory, chronology, store, first_rows = apply_audit_compactions(
            root,
            {"scene_compactions": [one_scene(1, 15, status="open", suffix=", при этом сцена ещё продолжается")]},
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )
        storage._write_json(root / SCENE_MEMORY_FILE, store)
        scene_id = first_rows[0]["scene_id"]

        memory, chronology, store, second_rows = apply_audit_compactions(
            root,
            {
                "scene_compactions": [
                    one_scene(
                        16,
                        30,
                        scene_id=scene_id,
                        status="closed",
                        suffix=", после чего эта же сцена получила завершение без смены эпизода",
                    )
                ]
            },
            start_turn=16,
            end_turn=30,
            memory=memory,
            chronology=chronology,
        )
        storage._write_json(root / SCENE_MEMORY_FILE, store)

        assert second_rows[0]["scene_id"] == scene_id
        assert len(store["scenes"]) == 1
        scene = store["scenes"][0]
        assert scene["scene_id"] == scene_id
        assert scene["start_turn"] == 1
        assert scene["end_turn"] == 30
        assert scene["source_ranges"] == [[1, 15], [16, 30]]
        assert scene["status"] == "closed"
        assert "завершение" in scene["summary"]
        assert covered_turns(root) == set(range(1, 31))


def test_scene_compaction_rejects_a_gap_in_the_fifteen_turn_range():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))

        with pytest.raises(RuntimeError, match="SCENE_COMPACTION_COVERAGE_INVALID"):
            apply_audit_compactions(
                root,
                {
                    "scene_compactions": [
                        one_scene(1, 7),
                        one_scene(9, 15),
                    ]
                },
                start_turn=1,
                end_turn=15,
                memory=memory,
                chronology=[],
            )


def test_writer_packet_uses_scene_history_and_does_not_retransmit_full_state_snapshots():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        huge_patch = {
            "relationship_documents": {"npc": {"history": "R" * 30_000}},
            "world": {"blob": "W" * 20_000},
            "threads": {"thread": {"notes": "T" * 15_000}},
            "current": {"location": "room", "present_characters": ["pov", "npc"]},
        }
        turns = []
        for turn in range(1, 18):
            turns.append(
                {
                    "turn_number": turn,
                    "user_input": f"input {turn}",
                    "scene_output": f"scene {turn}",
                    "extracted": {
                        "persistence_reviewed": True,
                        "chronology": [{"event_id": f"e{turn}", "event": f"event {turn}"}],
                        "knowledge_add": [],
                        "experiences_add": [],
                        "dialogue_memory_add": [],
                        "state_patch": huge_patch,
                    },
                }
            )
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
            encoding="utf-8",
        )
        storage._write_json(
            root / SCENE_MEMORY_FILE,
            {
                "version": 1,
                "scenes": [
                    {
                        "scene_id": "scene_000001",
                        "start_turn": 1,
                        "end_turn": 15,
                        "summary": one_scene(1, 15)["summary"],
                        "status": "closed",
                        "participants": ["pov", "npc"],
                        "locations": ["room"],
                        "source_ranges": [[1, 15]],
                    }
                ],
            },
        )
        meta = storage._read_json(root / "meta.json", {})
        meta.update({"turn_number": 17, "last_audit_turn": 15, "audit_required": False})
        storage._write_json(root / "meta.json", meta)

        manifest = session_runtime.prepare_turn_packet(sid, "next")
        parts = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            parts.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
        context = json.loads("".join(parts))

        assert len(context["scene_history"]) == 1
        assert context["scene_history"][0]["source_ranges"] == [[1, 15]]
        assert [row["turn_number"] for row in context["recent_turns"]] == [16, 17]
        assert context["continuity_turns"] == []
        recent_text = json.dumps(context["recent_turns"], ensure_ascii=False)
        assert "relationship_documents" not in recent_text
        assert "\"blob\"" not in recent_text
        assert "\"threads\"" not in recent_text
        assert len(recent_text) < 10_000
