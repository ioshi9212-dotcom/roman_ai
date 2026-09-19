import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.scene_compaction_runtime import SCENE_MEMORY_FILE


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "anchor-after-compaction",
        "title": "Anchor After Compaction",
        "novel": {"pov_character": "pov"},
        "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "room", "present_characters": ["pov"]},
        },
    }


def test_compacted_critical_event_stays_in_bounded_anchor_catalog():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        storage._write_json(root / "chronology.json", [
            {
                "event_id": "critical-4",
                "turn_number": 4,
                "event": "Критическое событие, которое нельзя потерять после scene compaction.",
                "importance": "critical",
                "anchor": True,
                "compacted_scene_id": "scene_000001",
                "location": "room",
            },
            {
                "event_id": "ordinary-5",
                "turn_number": 5,
                "event": "Обычное событие уже покрыто сценой.",
                "importance": "normal",
                "compacted_scene_id": "scene_000001",
            },
        ])
        storage._write_json(root / SCENE_MEMORY_FILE, {
            "version": 1,
            "scenes": [{
                "scene_id": "scene_000001",
                "start_turn": 1,
                "end_turn": 15,
                "summary": "POV провёл длинную сцену, внутри которой произошло критическое событие и несколько обычных эпизодов, после чего сцена завершилась.",
                "status": "closed",
                "participants": ["pov"],
                "locations": ["room"],
                "source_ranges": [[1, 15]],
            }],
        })
        meta = storage._read_json(root / "meta.json", {})
        meta.update({"turn_number": 15, "last_audit_turn": 15, "audit_required": False})
        storage._write_json(root / "meta.json", meta)

        manifest = session_runtime.prepare_turn_packet(sid, "next")
        parts = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            parts.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
        context = json.loads("".join(parts))

        anchors = context["chronology_anchor_catalog"]
        assert len(anchors) == 1
        assert anchors[0]["event_id"] == "critical-4"
        assert anchors[0]["compacted_scene_id"] == "scene_000001"
        assert "Критическое событие" in anchors[0]["summary"]
        assert all(row.get("event_id") != "ordinary-5" for row in anchors)
        assert all(row.get("event_id") != "ordinary-5" for row in context["chronology_recent"])
