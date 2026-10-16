"""The lean writer path must not reread long JSONL histories per scene."""
import json

from app import session_documents, storage
from app.private_knowledge_runtime import _history_turns
from app.writer_first_runtime import _rolling_turn_context


def test_reverse_archive_reader_preserves_utf8_and_filters(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    archive = session_documents.file_path(root, "turns.jsonl")
    archive.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "turn_number": number,
            "user_input": f"Сцена №{number} 漢字",
            "scene_output": "🍵" * 9000 if number == 32 else f"scene {number}",
        }
        for number in range(1, 71)
    ]
    # No newline at EOF; UTF-8 may cross the reverse reader's 64KB window.
    archive.write_bytes("\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode())
    assert storage._read_turns_backwards(root, count=4) == rows[-4:]
    assert storage._read_turns_backwards(root, count=3, excluded_numbers={69, 70}) == rows[65:68]
    assert storage._read_turns_backwards(root, wanted_numbers={2, 32, 69}) == [rows[1], rows[31], rows[68]]
    assert storage._read_turns_backwards(root, wanted_numbers=set()) == []


def test_writer_window_and_targeted_privacy_history_are_bounded(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    archive = session_documents.file_path(root, "turns.jsonl")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("".join(
        json.dumps({"turn_number": n, "user_input": f"акт {n}",
                    "scene_output": f"сцена {n}"}, ensure_ascii=False) + "\n"
        for n in range(1, 151)
    ), encoding="utf-8")

    recent, continuity = _rolling_turn_context(root)
    assert [item["turn_number"] for item in recent] == [149, 150]
    assert [item["turn_number"] for item in continuity] == list(range(136, 149))
    assert [item["turn_number"] for item in _history_turns(root, {136, 150})] == [136, 150]
    assert len(_history_turns(root)) == 150


def test_journalled_append_preserves_archive_on_later_write_failure(tmp_path, monkeypatch):
    import pytest
    from app import transactional_storage as transaction
    root = tmp_path / "session"
    root.mkdir()
    archive = root / "history.jsonl"
    archive.write_text('{"turn_number":1}\n', encoding="utf-8")
    original_replace = transaction.os.replace

    def fail_later(src, dst):
        if str(dst).endswith("/zz.json"):
            raise OSError("simulated write interruption")
        return original_replace(src, dst)

    monkeypatch.setattr(transaction.os, "replace", fail_later)
    with pytest.raises(OSError, match="simulated write interruption"):
        transaction.write_batch(
            root, {"zz.json": '{"ok":true}'},
            append_values={"history.jsonl": '{"turn_number":2}\n'},
        )
    assert archive.read_text(encoding="utf-8") == '{"turn_number":1}\n'
    assert not (root / "zz.json").exists()


def test_atomic_append_and_recovery_keep_original_turns(tmp_path):
    from app import transactional_storage as transaction
    root = tmp_path / "session"
    root.mkdir()
    transaction.write_batch(root, {}, append_values={"history.jsonl": "first\n"})
    transaction.write_batch(root, {"state.json": '{"x":1}\n'},
                            append_values={"history.jsonl": "second\n"})
    assert (root / "history.jsonl").read_text() == "first\nsecond\n"
    assert (root / "state.json").read_text() == '{"x":1}\n'
    transaction.recover(root)
    assert (root / "history.jsonl").read_text() == "first\nsecond\n"
