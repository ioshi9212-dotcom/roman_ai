import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_all_true_anchors_survive_beyond_old_24_event_limit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "anchors",
            "title": "Anchors",
            "novel": {"pov_character": "pov"},
            "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "nowhere", "present_characters": ["pov"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        chronology = []
        for turn in range(1, 41):
            chronology.append(
                {
                    "event_id": f"anchor_{turn}",
                    "turn_number": turn,
                    "event": f"Permanent milestone {turn}",
                    "importance": "anchor",
                    "participants_present": [],
                    "location": f"old-place-{turn}",
                }
            )
        for turn in range(41, 91):
            chronology.append(
                {
                    "event_id": f"normal_{turn}",
                    "turn_number": turn,
                    "event": f"Routine event {turn}",
                    "importance": "normal",
                    "participants_present": [],
                    "location": f"other-{turn}",
                }
            )
        storage._write_json(root / "chronology.json", chronology)

        manifest = session_runtime.prepare_turn_packet(sid, "Продолжить.")
        raw = "".join(storage._read_json(root / "turn_packet.json", {})["chunks"])
        context = json.loads(raw)
        ids = {event["event_id"] for event in context["chronology_recent"]}

        assert {f"anchor_{turn}" for turn in range(1, 41)}.issubset(ids)
        assert "anchor_1" in ids
        assert "anchor_40" in ids
        assert manifest["chronology_context_count"] >= 40


def test_major_events_remain_bounded_while_true_anchors_are_durable():
    events = [
        {"event_id": f"major_{turn}", "turn_number": turn, "event": "major", "importance": "major"}
        for turn in range(1, 61)
    ]
    events.append({"event_id": "critical_old", "turn_number": 1, "event": "critical", "importance": "critical"})
    selected = session_runtime._select_chronology_context(
        events,
        relevant_character_ids=[],
        location=None,
    )
    ids = {event["event_id"] for event in selected}
    assert "critical_old" in ids
    assert len([event_id for event_id in ids if event_id.startswith("major_")]) <= session_runtime.ANCHOR_CHRONOLOGY_EVENTS
