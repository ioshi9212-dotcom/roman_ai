"""Regression suite for the NEW opt-in audit mode; old sessions stay untouched."""
import json
import tempfile
from pathlib import Path

import pytest

from app import (
    audit_runtime, fast_audit_runtime, session_documents,
    session_runtime, storage,
)
from app.safe_audit_v2 import enabled, macro_knowledge_review
from app.scene_compaction_runtime import active_memory_records, apply_audit_compactions


def setup(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "safe-audit-v2",
        "title": "Audit v2 regression",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "silas", "name": "Сайлас"},
            {"character_id": "away", "name": "Отсутствующий"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026", "time": "10:00",
                "location": "room", "present_characters": ["pov", "silas"],
            },
        },
    }


def make(safe=True):
    return storage.create_session(
        novel(), meta_patch={"safe_audit_v2": True} if safe else None
    )["session_id"]


def packet(sid, user_input):
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(
            sid, manifest["packet_id"], index
        )["content"])
    return manifest, json.loads("".join(parts))


def scene(first, last):
    return [{
        "start_turn": first,
        "end_turn": last,
        "summary": (
            f"Ходы {first}–{last} показывают непрерывную тестовую сцену; "
            "персонажи последовательно обрабатывают последствия событий, "
            "оставляя исходные реплики в неизменяемом архиве."
        ),
        "participants": ["pov", "silas"],
        "location": "room",
        "status": "closed",
    }]


def play(sid, first, last):
    for turn in range(first, last + 1):
        raw = f"Тестовое действие {turn}"
        packet(sid, raw)
        result = session_runtime.commit_turn(sid, {
            "user_input": raw,
            "scene_output": (
                f"🎭 Audit v2\n🕒 День 1 · 01.09.2026, 10:00\n\n"
                f"Сцена {turn}.\n\nСостояние: нормально\nОтношения:\n\n"
                f"Ход {turn} · цикл {((turn - 1) % 15) + 1}/15"
            ),
            "extracted": {
                "persistence_reviewed": True,
                "chronology": [], "knowledge_add": [],
                "experiences_add": [], "dialogue_memory_add": [],
            },
        })
        assert result["turn_number"] == turn


def read_audit(sid):
    manifest = audit_runtime.get_audit_snapshot(sid)
    assert manifest["first_chunk_included"] is True
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(audit_runtime.get_audit_snapshot_chunk(
            sid, manifest["audit_id"], index,
        )["content"])
    return manifest, json.loads("".join(parts))


def test_legacy_session_stays_nonblocking_after_15():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        sid = make(safe=False)
        meta_path = storage.SESSIONS_DIR / sid / "meta.json"
        meta = storage._read_json(meta_path, {})
        assert enabled(meta) is False
        meta.update(turn_number=15, audit_required=True, last_audit_turn=0)
        storage._write_json(meta_path, meta)
        manifest, _ = packet(sid, "Новая сцена без обязательного аудита")
        assert manifest["prepared_for_turn"] == 16


def test_opted_in_new_session_blocks_turn_16_until_15_audited():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        sid = make(safe=True)
        meta_path = storage.SESSIONS_DIR / sid / "meta.json"
        assert enabled(storage._read_json(meta_path, {}))
        play(sid, 1, 15)
        assert storage._read_json(meta_path, {})["audit_required"]
        with pytest.raises(RuntimeError, match="AUDIT_REQUIRED"):
            packet(sid, "Ход 16 запрещён до аудита")
        from app.main import turn_packet_prepare
        from app.models import TurnPrepare
        routed = turn_packet_prepare(sid, TurnPrepare(user_input="Ход 16 запрещён до аудита"))
        assert routed["audit_required"] is True
        assert routed["required_audit"]["audit_range"] == [1, 15]
        manifest, evidence = read_audit(sid)
        assert evidence["audit_range"] == [1, 15]
        assert evidence["turn_evidence_backup"][-1]["turn_number"] == 15
        from app.main import turns_commit
        from app.models import CommitTurnRequest
        result = turns_commit(sid, CommitTurnRequest(
            audit_id=manifest["audit_id"], start_turn=1, end_turn=15,
            repairs={"scene_compactions": scene(1, 15)},
            notes=[],
        ))
        assert result["audited_through"] == 15
        audit_runtime.clear_audit_packet(sid)
        after, _ = packet(sid, "Ход 16 теперь разрешён")
        assert after["prepared_for_turn"] == 16
        assert len(storage._read_turns(storage.SESSIONS_DIR / sid)) == 15


def test_compacted_knowledge_and_journal_show_only_active_records_but_raw_stays():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        sid = make(safe=True)
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge"].extend([
            {"fact_id": "s1", "character_id": "silas", "learned_turn": 2,
             "fact": "Сайлас узнал код 42."},
            {"fact_id": "s2", "character_id": "silas", "learned_turn": 7,
             "fact": "Сайлас узнал время встречи 17:30."},
        ])
        bucket["knowledge_journal"].extend([
            {"entry_id": "j1", "turn": 3, "text": "Сайлас помнит адрес 23."},
            {"entry_id": "j2", "turn": 8, "text": "Сайлас помнит дату 12.10.2026."},
        ])
        repairs = {
            "scene_compactions": scene(1, 15),
            "memory_compactions": [
                {
                    "character_id": "silas", "memory_type": "knowledge",
                    "source_ids": ["s1", "s2"],
                    "summary": "Сайлас узнал код 42 и время встречи 17:30.",
                },
                {
                    "character_id": "silas", "memory_type": "knowledge_journal",
                    "source_ids": ["j1", "j2"],
                    "summary": "Сайлас помнит адрес 23 и дату 12.10.2026.",
                },
            ],
        }
        compressed, _, _, _ = apply_audit_compactions(
            root, repairs, start_turn=1, end_turn=15,
            memory=memory, chronology=[],
        )
        assert len(active_memory_records(compressed["characters"]["silas"]["knowledge"])) == 1
        assert len(active_memory_records(compressed["characters"]["silas"]["knowledge_journal"])) == 1
        storage._write_json(root / "memory.json", compressed)
        persisted = storage._read_json(root / "memory.json", {})
        assert {row["fact_id"] for row in persisted["characters"]["silas"]["knowledge"]} >= {
            "s1", "s2"
        }
        raw = session_documents.read(root / "память" / "silas.json")
        assert "Сайлас узнал код 42." in json.dumps(raw, ensure_ascii=False)
        public = session_documents.writer_documents(root, ["silas"])["personal_memory"]["silas"]
        assert "знания" in public
        rendered = json.dumps(public, ensure_ascii=False)
        assert "Сайлас узнал код 42 и время встречи 17:30." in rendered
        assert "Сайлас помнит адрес 23 и дату 12.10.2026." in rendered
        assert "Сайлас узнал код 42." not in rendered
        assert "Сайлас помнит адрес 23." not in rendered
        assert "Сайлас помнит дату 12.10.2026." not in rendered
        context_manifest, context = packet(sid, "Вспомнить Сайласу важное")
        assert context_manifest["chunk_chars_max"] == 16000
        active = json.dumps(context["character_memory"]["silas"], ensure_ascii=False)
        assert "Сайлас узнал код 42 и время встречи 17:30." in active
        assert "Сайлас узнал код 42." not in active
        assert set(context["character_memory"]) == {"pov", "silas"}
        assert "away" not in context["character_memory"]


def test_60_turn_macro_must_review_every_characters_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        sid = make(safe=True)
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta.update(turn_number=60, last_audit_turn=45, audit_required=True)
        storage._write_json(root / "meta.json", meta)
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        storage._memory_bucket(memory, "away")["knowledge_journal"].append({
            "entry_id": "hidden-away", "text": "Только отсутствующий знает пароль 876.",
            "turn": 1,
        })
        storage._write_json(root / "memory.json", memory)
        manifest, evidence = read_audit(sid)
        macro = evidence["macro_audit_60"]
        assert macro["required_character_ids"] == ["pov", "silas", "away"]
        assert "Только отсутствующий знает пароль 876." in json.dumps(
            macro["full_personal_memory_review"]["away"], ensure_ascii=False
        )
        assert "full_chronology_review" in macro
        assert "repairs.macro_knowledge_review" in macro["output_required"]
        missing_review = {
            "start_turn": 46, "end_turn": 60, "repairs": {
                "scene_compactions": scene(46, 60),
                "chronology_compactions": [],
            },
            "notes": [],
        }
        with pytest.raises(RuntimeError, match="MACRO_KNOWLEDGE_REVIEW_REQUIRED"):
            session_runtime.commit_audit(sid, missing_review)
        assert storage._read_json(root / "meta.json", {})["last_audit_turn"] == 45
        with pytest.raises(RuntimeError, match="MACRO_KNOWLEDGE_REVIEW_INCOMPLETE"):
            session_runtime.commit_audit(sid, {
                **missing_review,
                "repairs": {
                    **missing_review["repairs"], "memory_compactions": [],
                    "macro_knowledge_review": {"reviewed_character_ids": ["pov"]},
                },
            })


def test_lossy_compaction_rejected_without_changing_originals():
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        sid = make(safe=True)
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge"] = [
            {"fact_id": "a", "learned_turn": 2, "fact": "Он запомнил код 934."},
            {"fact_id": "b", "learned_turn": 5, "fact": "Он назначил встречу на 14:20."},
        ]
        with pytest.raises(RuntimeError, match="MEMORY_COMPACTION_CRITICAL_LITERAL_LOST"):
            apply_audit_compactions(
                root, {
                    "scene_compactions": scene(1, 15),
                    "memory_compactions": [{
                        "character_id": "silas", "memory_type": "knowledge",
                        "source_ids": ["a", "b"],
                        "summary": "Он запомнил код и договорился о встрече.",
                    }],
                }, start_turn=1, end_turn=15,
                memory=memory, chronology=[],
            )
        assert not any(row.get("superseded_by") for row in bucket["knowledge"])


def test_env_flag_only_marks_sessions_created_after_opt_in(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        setup(tmp)
        monkeypatch.delenv("ROMAN_SAFE_AUDIT_NEW_SESSIONS", raising=False)
        older = make(safe=False)
        monkeypatch.setenv("ROMAN_SAFE_AUDIT_NEW_SESSIONS", "1")
        newer = make(safe=False)
        assert not enabled(storage._read_json(storage.SESSIONS_DIR / older / "meta.json", {}))
        assert enabled(storage._read_json(storage.SESSIONS_DIR / newer / "meta.json", {}))
