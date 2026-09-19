import json
import tempfile
from copy import deepcopy
from pathlib import Path

from app import storage
from app.operation_service import prepare_turn_request
from app.scene_archive_read import (
    MAX_WORKING_SCENES,
    get_scene_archive_chunk,
    prepare_scene_archive_read,
)
from app.scene_compaction_runtime import SCENE_MEMORY_FILE, load_scene_history


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "consolidated-scene-archive",
        "title": "Consolidated Scene Archive",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "npc", "name": "NPC"},
        ],
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


def read_packet(session_id: str, manifest: dict):
    parts = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if parts else 0
    for index in range(start, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts)), "".join(parts)


def read_archive(session_id: str, scene_id=None):
    manifest = prepare_scene_archive_read(session_id, scene_id)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(
            get_scene_archive_chunk(
                session_id,
                scene_id,
                manifest["read_id"],
                index,
            )["content"]
        )
    return manifest, json.loads("".join(parts))


def seed_scenes(session_id: str, count: int = 30):
    root = storage.SESSIONS_DIR / session_id
    scenes = [scene_row(index) for index in range(1, count + 1)]
    storage._write_json(root / SCENE_MEMORY_FILE, {"version": 1, "scenes": scenes})
    meta = storage._read_json(root / "meta.json", {})
    meta.update({"turn_number": count * 15, "last_audit_turn": count * 15, "audit_required": False})
    storage._write_json(root / "meta.json", meta)
    return root, scenes


def test_legacy_client_keeps_full_scene_history_until_archive_capability_is_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root, scenes = seed_scenes(sid)

        before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))
        manifest = prepare_turn_request(sid, "legacy turn")
        context, raw = read_packet(sid, manifest)

        assert len(context["scene_history"]) == len(scenes) == 30
        assert manifest["scene_archive_capable"] is False
        assert "scene_history_window" not in context
        assert "ARCHIVE_MARKER_10" in raw
        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == before


def test_archive_capable_client_gets_bounded_working_history_without_deleting_archive():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root, scenes = seed_scenes(sid)

        before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))
        manifest = prepare_turn_request(
            sid,
            "bounded turn",
            "req-scene-bounded",
            scene_archive_capable=True,
        )
        context, raw = read_packet(sid, manifest)

        assert manifest["scene_history_bounded"] is True
        assert manifest["scene_archive_capable"] is True
        assert len(context["scene_history"]) <= MAX_WORKING_SCENES
        assert context["scene_history_window"]["persistent_scene_count"] == 30
        assert context["scene_history_window"]["working_scene_count"] == len(context["scene_history"])
        assert context["scene_history_window"]["omitted_scene_count"] == 30 - len(context["scene_history"])
        assert any(row["scene_id"] == "scene_000001" for row in context["scene_history"])
        assert any(row["scene_id"] == "scene_000030" for row in context["scene_history"])
        assert "ARCHIVE_MARKER_10" not in raw

        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == before
        assert load_scene_history(root) == scenes


def test_complete_scene_index_reconstructs_losslessly_from_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root, scenes = seed_scenes(sid)
        before = deepcopy(storage._read_json(root / SCENE_MEMORY_FILE, {}))

        manifest, payload = read_archive(sid)

        assert payload["mode"] == "scene_index"
        assert payload["persistent_scene_count"] == 30
        assert payload["scenes"] == scenes
        assert payload["raw_turns_included"] is False
        assert manifest["chunk_count"] >= 2
        assert storage._read_json(root / SCENE_MEMORY_FILE, {}) == before


def test_exact_old_scene_read_returns_raw_turn_evidence_and_reports_completeness():
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
                "extracted": {"chronology": []},
            }
            for turn in range(1, 16)
        ]
        (root / "turns.jsonl").write_text(
            "\n".join(json.dumps(turn, ensure_ascii=False) for turn in turns) + "\n",
            encoding="utf-8",
        )
        raw_before = (root / "turns.jsonl").read_text(encoding="utf-8")

        manifest, payload = read_archive(sid, scene["scene_id"])

        assert manifest["chunk_count"] >= 2
        assert payload["mode"] == "scene_evidence"
        assert payload["scene"] == scene
        assert payload["source_ranges"] == [[1, 15]]
        assert payload["raw_turn_evidence_complete"] is True
        assert payload["missing_raw_turn_numbers"] == []
        assert [row["turn_number"] for row in payload["raw_turns"]] == list(range(1, 16))
        assert (root / "turns.jsonl").read_text(encoding="utf-8") == raw_before
