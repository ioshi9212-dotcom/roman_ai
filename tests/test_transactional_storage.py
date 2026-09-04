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
