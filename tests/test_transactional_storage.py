import json
import tempfile
from pathlib import Path

from app.transactional_storage import recover, write_batch


def test_write_batch_updates_multiple_files_together():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "state.json").write_text('{"value": 1}\n', encoding="utf-8")
        (root / "meta.json").write_text('{"turn": 1}\n', encoding="utf-8")

        write_batch(
            root,
            {
                "state.json": '{"value": 2}\n',
                "meta.json": '{"turn": 2}\n',
                "memory.json": '{"ok": true}\n',
            },
        )

        assert json.loads((root / "state.json").read_text(encoding="utf-8"))["value"] == 2
        assert json.loads((root / "meta.json").read_text(encoding="utf-8"))["turn"] == 2
        assert json.loads((root / "memory.json").read_text(encoding="utf-8"))["ok"] is True
        assert not (root / ".transactions").exists()


def test_recover_rolls_back_interrupted_prepared_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        target = root / "state.json"
        target.write_text('{"value": "before"}\n', encoding="utf-8")

        tx = root / ".transactions" / "deadbeef"
        backup = tx / "backup" / "0000.txt"
        staged = tx / "staged" / "0000.txt"
        backup.parent.mkdir(parents=True)
        staged.parent.mkdir(parents=True)
        backup.write_text('{"value": "before"}\n', encoding="utf-8")
        staged.write_text('{"value": "after"}\n', encoding="utf-8")

        # Simulate a crash after the target was already replaced but before commit marker.
        target.write_text('{"value": "after"}\n', encoding="utf-8")
        manifest = {
            "version": 1,
            "state": "prepared",
            "entries": [
                {
                    "target": "state.json",
                    "staged": "staged/0000.txt",
                    "backup": "backup/0000.txt",
                    "backup_exists": True,
                }
            ],
        }
        (tx / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        recover(root)

        assert json.loads(target.read_text(encoding="utf-8"))["value"] == "before"
        assert not (root / ".transactions").exists()


def test_atomic_append_preserves_existing_journal_and_updates_other_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "turns.jsonl"
        old = '{"turn_number": 1, "scene_output": "Сцена один"}\n'
        new = '{"turn_number": 2, "scene_output": "Сцена два"}\n'
        path.write_text(old, encoding="utf-8")
        inode = path.stat().st_ino
        (root / "meta.json").write_text('{"turn_number": 1}\n', encoding="utf-8")
        write_batch(
            root, {"meta.json": '{"turn_number": 2}\n'},
            append_values={"turns.jsonl": new},
        )
        assert path.read_text(encoding="utf-8") == old + new
        assert path.stat().st_ino == inode  # no rewrite of full history
        assert json.loads((root / "meta.json").read_text())["turn_number"] == 2
        assert not (root / ".transactions").exists()


def test_prepared_append_crash_recovery_preserves_archive_and_other_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        journal = root / "turns.jsonl"
        old = ('{"turn": 1, "text": "Сцена"}\n' * 50)
        journal.write_text(old, encoding="utf-8")
        original_size = journal.stat().st_size
        state = root / "state.json"
        state.write_text('{"value": "before"}\n', encoding="utf-8")
        tx = root / ".transactions" / "interrupted"
        (tx / "backup").mkdir(parents=True)
        (tx / "staged").mkdir()
        (tx / "backup" / "0000.txt").write_text('{"value": "before"}\n', encoding="utf-8")
        (tx / "staged" / "0000.txt").write_text('{"value": "after"}\n', encoding="utf-8")
        (tx / "staged" / "0001.txt").write_text('{"turn": 51}\n', encoding="utf-8")
        # Crash after replacing state and partially appending the journal.
        state.write_text('{"value": "after"}\n', encoding="utf-8")
        with journal.open("ab") as handle:
            handle.write(b'{"turn": 51')
        (tx / "manifest.json").write_text(json.dumps({
            "version": 1, "state": "prepared",
            "entries": [
                {"target": "state.json", "staged": "staged/0000.txt",
                 "backup": "backup/0000.txt", "backup_exists": True},
                {"target": "turns.jsonl", "operation": "append",
                 "staged": "staged/0001.txt", "backup_exists": True,
                 "original_size": original_size},
            ],
        }), encoding="utf-8")
        recover(root)
        assert journal.read_text(encoding="utf-8") == old
        assert state.read_text(encoding="utf-8") == '{"value": "before"}\n'
        assert not (root / ".transactions").exists()


def test_append_transaction_rejects_duplicate_target_without_touching_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "turns.jsonl"
        path.write_text("original\n", encoding="utf-8")
        import pytest
        with pytest.raises(ValueError, match="cannot be replaced and appended"):
            write_batch(root, {"turns.jsonl": "replacement\n"},
                        append_values={"turns.jsonl": "append\n"})
        assert path.read_text(encoding="utf-8") == "original\n"


def test_append_transaction_rollback_on_write_failure(monkeypatch):
    from app import transactional_storage

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        journal = root / "turns.jsonl"
        old = '{"turn": 1}\n' * 200
        journal.write_text(old, encoding="utf-8")
        state = root / "state.json"
        state.write_text('{"turn": 1}\n', encoding="utf-8")
        original_fsync_dir = transactional_storage._fsync_dir
        triggered = []

        def fail_after_append(path):
            # Replacement files are already staged/installed here; ensure
            # failure triggers full rollback of both kinds of writes.
            if Path(path) == root and journal.stat().st_size > len(old.encode("utf-8")):
                triggered.append(True)
                raise OSError("simulated fsync failure after journal append")
            return original_fsync_dir(path)

        monkeypatch.setattr(transactional_storage, "_fsync_dir", fail_after_append)
        import pytest
        with pytest.raises(OSError, match="simulated fsync"):
            write_batch(
                root, {"state.json": '{"turn": 2}\n'},
                append_values={"turns.jsonl": '{"turn": 2}\n'},
            )
        assert triggered
        assert journal.read_text(encoding="utf-8") == old
        assert state.read_text(encoding="utf-8") == '{"turn": 1}\n'
        assert not (root / ".transactions").exists()
