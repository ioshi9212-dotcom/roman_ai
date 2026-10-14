"""The ordinary scene path must stay lossless without repeated work or audit gates."""
from collections import Counter
import json
from pathlib import Path

import pytest

from app import fast_audit_runtime, operation_service, session_documents, session_migrations, session_runtime, storage, transactional_storage
from test_generator_state_documents import create, prepare, story


def payload(manifest, text="Продолжаю разговор"):
    return {"packet_id": manifest["packet_id"], "user_input": text,
            "scene_output": "**Рен** — Я слушаю.", "extracted": {}}


def test_prepare_reads_archive_once_and_does_not_open_offscreen_memory(story, monkeypatch):
    sid, root = create(story)
    session_migrations.ensure_current_session_data(sid)
    reads = Counter()
    original = Path.read_text
    def tracked(path, *args, **kwargs):
        if path.is_relative_to(root):
            reads[path.relative_to(root).as_posix()] += 1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", tracked)
    _, context = prepare(sid)
    assert reads["ходы.jsonl"] == 1
    assert reads["память/away.json"] == 0
    assert reads["память/ren.json"] == 1
    assert set(context["character_memory"]) == {"pov", "ren"}
    assert "away" not in context["character_memory"]
    assert context["runtime_rules"] and context["scene_builder"]
    assert "persistence_contract" not in context
    assert "scene_characters" not in context
    assert "knowledge_boundary" not in context


def test_commit_appends_exact_archive_and_replay_does_not_append_twice(story, monkeypatch):
    sid, root = create(story)
    first, _ = prepare(sid)
    operation_service.commit_turn_request(sid, payload(first))
    archive = root / "ходы.jsonl"
    prefix = archive.read_bytes()
    second, _ = prepare(sid, text="Второй ход", request_id="second")
    reads = Counter()
    original = Path.read_text
    def tracked(path, *args, **kwargs):
        if path == archive:
            reads["archive"] += 1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", tracked)
    body = payload(second, "Второй ход")
    saved = operation_service.commit_turn_request(sid, body)
    assert reads["archive"] == 1
    assert saved["turn_number"] == 2
    assert archive.read_bytes().startswith(prefix)
    after = archive.read_bytes()
    assert operation_service.commit_turn_request(sid, body)["already_committed"] is True
    assert archive.read_bytes() == after
    assert len(storage._read_turns(root)) == 2


@pytest.mark.parametrize("crash", [False, True])
def test_failed_archive_append_restores_all_documents_and_can_retry(story, monkeypatch, crash):
    sid, root = create(story)
    manifest, _ = prepare(sid)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
              if path.is_file() and path.name != ".session.lock"}
    original = transactional_storage._append_staged
    def fail(target, staged):
        original(target, staged)
        if crash:
            raise SystemExit("simulated process loss")
        raise OSError("simulated append failure")
    with monkeypatch.context() as patch:
        patch.setattr(transactional_storage, "_append_staged", fail)
        with pytest.raises(SystemExit if crash else OSError):
            operation_service.commit_turn_request(sid, payload(manifest))
    transactional_storage.recover(root)
    for name, raw in before.items():
        assert (root / name).read_bytes() == raw, name
    result = operation_service.commit_turn_request(sid, payload(manifest))
    assert result["turn_number"] == 1
    assert len(storage._read_turns(root)) == 1


@pytest.mark.parametrize("version", [1, 5])
def test_checkpoint_and_resume_do_not_require_audit(story, monkeypatch, version):
    story["version"] = version
    sid, root = create(story)
    def automatic_audit(*args, **kwargs):
        raise AssertionError("ordinary scenes must not build an audit packet")
    with monkeypatch.context() as patch:
        patch.setattr(fast_audit_runtime, "get_audit_snapshot", automatic_audit)
        for turn in range(1, 18):
            text = f"Продолжаю разговор {turn}"
            manifest, context = prepare(sid, text, request_id=f"turn-{turn}")
            assert not context["session"].get("audit_required")
            result = operation_service.commit_turn_request(sid, payload(manifest, text))
            assert result["turn_number"] == turn
            assert result["audit_blocking"] is False
            assert "required_audit" not in result
            if turn == 15:
                resumed = session_runtime.continue_session(sid)
                assert resumed["audit_available"] is True
                assert resumed["audit_required"] is False
                assert "required_audit" not in resumed
                assert "Complete required_audit" not in resumed["instruction"]
    assert len(storage._read_turns(root)) == 17
    assert not (root / "audit_packet.json").exists()
    # Explicit maintenance remains available, with exact original evidence.
    audit = fast_audit_runtime.get_audit_snapshot(sid)
    assert audit["audit_range"] == [3, 17]


def test_cached_views_see_transactional_changes(story):
    _, root = create(story)
    with session_documents.read_view():
        state = storage._read_json(root / "state.json", {})
        state["current"]["location"] = "Библиотека"
        storage._write_json(root / "state.json", state)
        assert storage._read_json(root / "state.json", {})["current"]["location"] == "Библиотека"


def test_cached_views_refresh_after_another_writer_between_locks(story):
    _, root = create(story)
    with session_documents.read_view():
        with transactional_storage.session_transaction(root):
            assert storage._read_json(root / "state.json", {})["current"]["location"] == "Кафе"
        # Simulate a different worker committing while this request has no lock.
        native = json.loads((root / "state.json").read_text())
        native["локация"]["название"] = "Мастерская"
        (root / "state.json").write_text(json.dumps(native, ensure_ascii=False))
        with transactional_storage.session_transaction(root):
            assert storage._read_json(root / "state.json", {})["current"]["location"] == "Мастерская"


def test_historical_deduplication_runs_once_during_upgrade(story):
    sid, root = create(story)
    meta = storage._read_json(root / "meta.json", {})
    meta["data_schema_version"] = 2
    storage._write_json(root / "meta.json", meta)
    memory = storage._read_json(root / "memory.json", {})
    row = {"entry_id": "old", "turn": 0, "date": "14.10.2026", "text": "Со слов Елены: она любит кофе."}
    memory["characters"]["ren"]["knowledge_journal"] = [row, {**row, "entry_id": "duplicate"}]
    storage._write_json(root / "memory.json", memory)
    manifest, _ = prepare(sid)
    assert len(storage._read_json(root / "memory.json", {})["characters"]["ren"]["knowledge_journal"]) == 1
    operation_service.commit_turn_request(sid, payload(manifest))
    memory = storage._read_json(root / "memory.json", {})
    memory["characters"]["ren"]["knowledge_journal"].append({**row, "entry_id": "later"})
    storage._write_json(root / "memory.json", memory)
    prepare(sid, "Второй ход", request_id="later")
    assert len(storage._read_json(root / "memory.json", {})["characters"]["ren"]["knowledge_journal"]) == 2
