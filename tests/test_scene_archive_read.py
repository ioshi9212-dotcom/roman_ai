import json
import tempfile
from copy import deepcopy
from pathlib import Path

from app import session_runtime, storage
from app.scene_archive_read import (
    SCENE_ARCHIVE_CHUNK_CHARS,
    get_scene_archive_chunk,
    prepare_scene_archive_read,
)
from app.scene_compaction_runtime import SCENE_MEMORY_FILE, load_scene_history
from app.writer_first_runtime import MAX_WORKING_SCENES


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "scene-archive",
        "title": "Scene Archive",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "npc", "name": "NPC"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov", "npc"]},
        },
    }


def scene_row(index: int):
    start = (index - 1) * 15 + 1
    end = index * 15
    return {
        "scene_id": f"scene_{index:06d}",
        "start_turn": start,
        "end_turn": end,
        "summary": f"ARCHIVE_MARKER_{index} " + ("S" * 520),
        "status": "closed",
        "participants": ["npc"] if index == 1 else ["other"],
        "locations": ["room"] if index == 1 else ["street"],
        "source_ranges": [[start, end]],
    }


def read_packet(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "next")
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(parts)), "".join(parts)


def read_archive(session_id: str, scene_id=None):
    manifest = prepare_scene_archive_read(session_id, scene_id)
    parts = [manifest["content"]]
    assert len(parts[0]) <= SCENE_ARCHIVE_CHUNK_CHARS
    for index in range(1, manifest["chunk_count"]):
        row = get_scene_archive_chunk(session_id, scene_id, manifest["read_id"], index)
        assert len(row["content"]) <= SCENE_ARCHIVE_CHUNK_CHARS
        parts.append(row["content"])
    return manifest, json.loads("".join(parts))


def test_writer_packet_bounds_scene_history_but_keeps_persistent_archive_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        scenes = [scene_row(index) for index in range(1, 31)]
        store = {"version": 1, "scenes": scenes}
        storage._write_json(root / SCENE_MEMORY_FILE, store)
        before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))

        meta = storage._read_json(root / "meta.json", {})
        meta.update({"turn_number": 450, "last_audit_turn": 450, "audit_required": False})
        storage._write_json(root / "meta.json", meta)

        manifest, context, raw = read_packet(sid)

        assert len(context["scene_history"]) <= MAX_WORKING_SCENES
        assert context["scene_history_window"]["persistent_scene_count"] == 30
        assert context["scene_history_window"]["working_scene_count"] == len(context["scene_history"])
        assert context["scene_history_window"]["omitted_scene_count"] == 30 - len(context["scene_history"])
        assert context["scene_history_window"]["complete_archive_persistent"] is True
        assert "prepareSceneArchiveRead" in json.dumps(context["scene_history_window"], ensure_ascii=False)
        assert any(row["scene_id"] == "scene_000001" for row in context["scene_history"])  # current NPC/location relevance
        assert any(row["scene_id"] == "scene_000030" for row in context["scene_history"])  # recent
        assert "ARCHIVE_MARKER_10" not in raw
        assert manifest["writer_first_version"] >= 10

        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == before
        assert len(load_scene_history(root)) == 30


def test_complete_scene_index_is_chunked_and_lossless_on_demand():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        scenes = [scene_row(index) for index in range(1, 31)]
        storage._write_json(root / SCENE_MEMORY_FILE, {"version": 1, "scenes": scenes})
        before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))

        manifest, payload = read_archive(sid)

        assert manifest["chunk_count"] >= 2
        assert payload["mode"] == "scene_index"
        assert payload["persistent_scene_count"] == 30
        assert payload["scenes"] == scenes
        assert payload["raw_turns_included"] is False
        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == before


def test_exact_old_scene_read_includes_every_raw_turn_without_mutating_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        scene = scene_row(1)
        storage._write_json(root / SCENE_MEMORY_FILE, {"version": 1, "scenes": [scene]})

        turns = [
            {
                "turn_number": turn,
                "user_input": f"RAW_INPUT_{turn}",
                "scene_output": f"RAW_SCENE_{turn}_" + ("X" * 1800),
                "extracted": {"chronology": [{"event_id": f"evt-{turn}", "event": f"raw event {turn}"}]},
            }
            for turn in range(1, 16)
        ]
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
            encoding="utf-8",
        )
        raw_before = (root / "turns.jsonl").read_text(encoding="utf-8")
        scene_before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))

        manifest, payload = read_archive(sid, scene["scene_id"])

        assert manifest["chunk_count"] >= 2
        assert payload["mode"] == "scene_evidence"
        assert payload["scene_id"] == scene["scene_id"]
        assert payload["scene"] == scene
        assert payload["source_ranges"] == [[1, 15]]
        assert payload["raw_turn_evidence_complete"] is True
        assert payload["missing_raw_turn_numbers"] == []
        assert [row["turn_number"] for row in payload["raw_turns"]] == list(range(1, 16))
        assert payload["raw_turns"][9]["scene_output"].startswith("RAW_SCENE_10_")

        assert (root / "turns.jsonl").read_text(encoding="utf-8") == raw_before
        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == scene_before
