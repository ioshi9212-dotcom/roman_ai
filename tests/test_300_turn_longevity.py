import json
import tempfile
from pathlib import Path

from app import audit_runtime, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    characters = [
        {"character_id": "pov", "name": "POV", "is_pov": True},
        {"character_id": "ren", "name": "Ren", "role": "investigator"},
    ]
    characters.extend(
        {"character_id": f"npc_{index}", "name": f"NPC {index}", "role": "support"}
        for index in range(38)
    )
    return {
        "novel_id": "longevity-300",
        "title": "Longevity 300",
        "novel": {"pov_character": "pov", "genre": ["romance", "mystery"]},
        "characters": characters,
        "lore": {"setting": "large persistent city"},
        "story_direction": {"macro": ["mystery", "relationship"]},
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "cafe", "scene": "conversation", "present_characters": ["pov", "ren"], "game_day": 1},
        },
    }


def populate(session_id: str, turn_count: int):
    root = storage.SESSIONS_DIR / session_id
    turns = []
    chronology = []
    for turn in range(1, turn_count + 1):
        turns.append(
            {
                "turn_number": turn,
                "user_input": f"player input {turn}",
                "scene_output": f"Scene {turn}. " + ("S" * 2300),
                "extracted": {
                    "chronology": [{"event_id": f"evt-{turn}", "event": f"event {turn}"}],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "npc_intent_updates": [],
                },
            }
        )
        chronology.append(
            {
                "event_id": f"evt-{turn}",
                "turn_number": turn,
                "event": f"Durable event {turn}",
                "participants_present": ["pov", "ren"],
                "location": "cafe" if turn % 4 else "street",
                "importance": "anchor" if turn % 37 == 0 else "normal",
            }
        )
    (root / "turns.jsonl").write_text(
        "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
        encoding="utf-8",
    )
    storage._write_json(root / "chronology.json", chronology)

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    for character_id in ("pov", "ren"):
        bucket = storage._memory_bucket(memory, character_id)
        for turn in range(1, turn_count + 1):
            bucket["knowledge"].append(
                {"fact_id": f"{character_id}-fact-{turn}", "learned_turn": turn, "fact": f"Known fact {turn} " + ("K" * 250)}
            )
            if turn % 2 == 0:
                bucket["experiences"].append(
                    {"event_id": f"{character_id}-exp-{turn}", "turn": turn, "summary": f"Experience {turn} " + ("E" * 250)}
                )
            if turn % 3 == 0:
                bucket["dialogue_memory"].append(
                    {"topic_id": f"{character_id}-dialogue-{turn}", "turn": turn, "summary": f"Dialogue {turn} " + ("D" * 250)}
                )
    storage._write_json(root / "memory.json", memory)

    state = storage._read_json(root / "state.json", {})
    state["current"]["game_day"] = max(1, turn_count // 8)
    state["threads"] = {
        f"thread-{index}": {
            "status": "closed" if index < turn_count // 3 else "active",
            "priority": index % 100,
            "notes": f"thread notes {index} " + ("T" * 2500),
        }
        for index in range(max(20, turn_count))
    }
    state["npc_intents"] = {
        "ren": [
            {
                "intent_id": f"intent-{index}",
                "character_id": "ren",
                "summary": f"Follow up on clue {index}",
                "status": "resolved" if index < max(0, turn_count // 20 - 8) else "active",
                "priority": 40 + index,
                "created_turn": max(1, turn_count - 80 + index),
                "created_game_day": max(1, turn_count // 8 - 4),
            }
            for index in range(max(8, turn_count // 15))
        ]
    }
    storage._write_json(root / "state.json", state)

    meta = storage._read_json(root / "meta.json", {})
    meta["turn_number"] = turn_count
    meta["last_audit_turn"] = turn_count
    meta["audit_required"] = False
    meta["handoff_required"] = False
    storage._write_json(root / "meta.json", meta)


def prepare_size(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "(посмотреть на Рена)")
    return manifest["total_chars"], manifest["chunk_count"]


def test_turn_300_writer_packet_stays_same_order_of_magnitude_as_turn_30():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        short_sid = storage.create_session(novel())["session_id"]
        long_sid = storage.create_session(novel())["session_id"]
        populate(short_sid, 30)
        populate(long_sid, 300)

        chars_30, chunks_30 = prepare_size(short_sid)
        chars_300, chunks_300 = prepare_size(long_sid)

        assert chunks_30 <= 8
        assert chunks_300 <= 8
        assert chars_300 <= chars_30 * 1.35
        assert chunks_300 <= chunks_30 + 2


def test_turn_300_fast_audit_is_compact_and_inlines_first_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        populate(sid, 300)
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta["last_audit_turn"] = 285
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        manifest = audit_runtime.get_audit_snapshot(sid)
        assert manifest["first_chunk_included"] is True
        assert manifest["chunk_count"] <= 5
        assert manifest["already_read_chunks"] == [0]
        pieces = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            pieces.append(audit_runtime.get_audit_snapshot_chunk(sid, manifest["audit_id"], index)["content"])
        payload = json.loads("".join(pieces))
        assert payload["audit_mode"] == "fast_chat_reconciliation"
        assert payload["audit_range"] == [286, 300]
        assert len(payload["turn_evidence_backup"]) == 15
        assert "audit_turns_full" not in payload
        audit_runtime.require_complete_audit_read(sid, 286, 300)
