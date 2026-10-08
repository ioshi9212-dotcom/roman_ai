"""Guarantee commit-scoped read caching never changes canonical data semantics."""
import json
import tempfile
from pathlib import Path

from app import storage


def test_session_read_snapshot_reuses_json_without_sharing_mutable_objects(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        storage._write_json(root / "state.json", {"current": {"time": "01:00"}})
        original = Path.read_text
        reads = []

        def traced(path, *args, **kwargs):
            if path.name == "state.json":
                reads.append(path.name)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", traced)
        with storage.session_read_snapshot(root):
            first = storage._read_json(root / "state.json", {})
            first["current"]["time"] = "99:99"
            second = storage._read_json(root / "state.json", {})
            assert second["current"]["time"] == "01:00"
            assert reads == ["state.json"]

            # A normal file write invalidates the snapshot immediately.
            storage._write_json(root / "state.json", {"current": {"time": "02:00"}})
            third = storage._read_json(root / "state.json", {})
            assert third["current"]["time"] == "02:00"
            assert reads == ["state.json", "state.json"]

        storage._read_json(root / "state.json", {})
        assert reads == ["state.json", "state.json", "state.json"]


def test_session_read_snapshot_tracks_turns_file_changes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "turns.jsonl"
        path.write_text(json.dumps({"turn_number": 1}) + "\n", encoding="utf-8")
        with storage.session_read_snapshot(root):
            first = storage._read_turns(root)
            first[0]["turn_number"] = -1
            assert storage._read_turns(root)[0]["turn_number"] == 1
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"turn_number": 2}) + "\n")
            assert [t["turn_number"] for t in storage._read_turns(root)] == [1, 2]


def test_session_snapshot_never_overrides_other_session_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        other = root / "other_session"
        other.mkdir()
        storage._write_json(other / "state.json", {"from": "other"})
        with storage.session_read_snapshot(root):
            assert storage._read_json(other / "state.json", {}) == {"from": "other"}
