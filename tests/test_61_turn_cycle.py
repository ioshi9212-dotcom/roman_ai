import tempfile
from pathlib import Path

from app import audit_runtime, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_turn_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    for index in range(manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)
    return manifest


def close_audit(session_id: str, expected_range):
    manifest = audit_runtime.get_audit_snapshot(session_id)
    assert manifest["audit_range"] == expected_range
    for index in range(manifest["chunk_count"]):
        audit_runtime.get_audit_snapshot_chunk(session_id, manifest["audit_id"], index)
    result = session_runtime.commit_audit(
        session_id,
        {"start_turn": expected_range[0], "end_turn": expected_range[1], "repairs": {}, "notes": []},
    )
    audit_runtime.clear_audit_packet(session_id)
    assert result["audited_through"] == expected_range[1]
    return result


def test_same_session_runs_through_45_and_60_without_handoff_block():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "cycle61",
            "title": "Cycle 61",
            "novel": {"pov_character": "pov"},
            "characters": [{"character_id": "pov", "name": "POV", "is_pov": True}],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"date": "01.09.2026", "time": "10:00", "location": "room", "present_characters": ["pov"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        audited = []

        for turn in range(1, 62):
            user_input = f"Ход {turn}."
            manifest = read_turn_packet(sid, user_input)
            assert manifest["prepared_for_turn"] == turn
            result = session_runtime.commit_turn(
                sid,
                {
                    "user_input": user_input,
                    "scene_output": f"🎭 Cycle 61 · осень\n🕒 День 1 · вторник, 01.09.2026, 10:00 · 📍 room\n\nСцена {turn}.\n\nСостояние: нормально\nОтношения:\n\nХод {turn} · цикл {((turn - 1) % 15) + 1}/15",
                    "extracted": {
                        "persistence_reviewed": True,
                        "chronology": [],
                        "knowledge_add": [],
                        "experiences_add": [],
                        "dialogue_memory_add": [],
                    },
                },
            )
            assert result["turn_number"] == turn
            assert result["handoff_required"] is False

            if turn % 15 == 0:
                audit_result = close_audit(sid, [turn - 14, turn])
                audited.append(turn)
                assert audit_result["handoff_required"] is False

            if turn == 45:
                # The exact failure point from the real incident: next packet must exist.
                next_manifest = session_runtime.prepare_turn_packet(sid, "probe-46")
                assert next_manifest["prepared_for_turn"] == 46
                (storage.SESSIONS_DIR / sid / "turn_packet.json").unlink(missing_ok=True)
            if turn == 60:
                meta = storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})
                assert meta.get("handoff_required") is not True

        assert audited == [15, 30, 45, 60]
        meta = storage._read_json(storage.SESSIONS_DIR / sid / "meta.json", {})
        assert meta["turn_number"] == 61
        assert meta["last_audit_turn"] == 60
        assert meta["audit_required"] is False
        assert meta.get("handoff_required") is not True
        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 61
