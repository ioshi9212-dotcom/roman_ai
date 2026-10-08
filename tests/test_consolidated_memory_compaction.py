import tempfile
from pathlib import Path

from app import storage
from app.character_chunk_read import _tail
from app.scene_compaction_runtime import active_memory_records, apply_audit_compactions, complete_knowledge_records
from app.turn_context import _working_records


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "memory-compaction-clean",
        "title": "Memory Compaction Clean",
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


def scene(start_turn: int, end_turn: int):
    return {
        "start_turn": start_turn,
        "end_turn": end_turn,
        "summary": (
            f"На ходах {start_turn}–{end_turn} POV и NPC продолжили одну сцену, обменялись важными фактами "
            "и действиями, после чего эпизод дошёл до зафиксированной точки продолжения."
        ),
        "participants": ["pov", "npc"],
        "location": "room",
        "status": "closed",
    }


def test_compaction_never_promotes_uncertain_knowledge_to_certain():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].extend([
            {
                "fact_id": "rumor",
                "character_id": "npc",
                "learned_turn": 2,
                "fact": "NPC услышал неподтверждённый слух.",
                "confidence": "uncertain",
            },
            {
                "fact_id": "confirmed-piece",
                "character_id": "npc",
                "learned_turn": 7,
                "fact": "Часть сообщения подтверждена.",
                "confidence": "certain",
            },
        ])

        memory, _, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["rumor", "confirmed-piece"],
                    "summary": "NPC хранит сообщение, часть которого подтверждена, но исходный слух остаётся неуверенным.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )

        current = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]
        assert current["confidence"] == "uncertain"
        assert current["source_confidences"] == [
            {"source_id": "rumor", "confidence": "uncertain"},
            {"source_id": "confirmed-piece", "confidence": "certain"},
        ]
        assert current["learned_turn"] == 2
        assert current["first_learned_turn"] == 2
        assert current["last_learned_turn"] == 7


def test_rolling_compaction_flattens_confidence_and_importance_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].extend([
            {
                "fact_id": "a",
                "character_id": "npc",
                "learned_turn": 2,
                "fact": "Первый фрагмент.",
                "confidence": "uncertain",
                "importance": "major",
                "durable": True,
            },
            {
                "fact_id": "b",
                "character_id": "npc",
                "learned_turn": 7,
                "fact": "Второй фрагмент.",
                "confidence": "certain",
                "importance": "anchor",
                "pinned": True,
            },
        ])

        memory, chronology, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["a", "b"],
                    "summary": "NPC объединяет первый и второй фрагмент, сохраняя исходную степень уверенности и важность.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )
        first = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]
        assert first["source_importance"] == ["major", "anchor"]

        memory["characters"]["npc"]["knowledge"].append({
            "fact_id": "c",
            "character_id": "npc",
            "learned_turn": 22,
            "fact": "Новый противоречивый слух.",
            "confidence": "rumor",
            "importance": "critical",
            "permanent": True,
        })

        memory, chronology, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(16, 30)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": [first["fact_id"], "c"],
                    "summary": "NPC хранит объединённую версию с подтверждёнными деталями и противоречивым новым слухом.",
                }],
            },
            start_turn=16,
            end_turn=30,
            memory=memory,
            chronology=chronology,
        )

        current = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]
        assert current["confidence"] == "mixed"
        assert current["source_confidences"] == [
            {"source_id": "a", "confidence": "uncertain"},
            {"source_id": "b", "confidence": "certain"},
            {"source_id": "c", "confidence": "rumor"},
        ]
        assert current["importance"] == "critical"
        assert current["source_importance"] == ["major", "anchor", "critical"]
        assert current["durable"] is True
        assert current["pinned"] is True
        assert current["permanent"] is True
        assert current["learned_turn"] == 2
        assert current["last_learned_turn"] == 22
        assert set(current["merged_from"]) == {"a", "b", "c"}
        assert set(current["source_turns"]) == {2, 7, 22}


def test_fresh_detail_keeps_old_canonical_fact_recent_in_both_working_paths():
    record = {
        "fact_id": "cmp",
        "fact": "Старое знание получило свежую деталь.",
        "learned_turn": 2,
        "first_learned_turn": 2,
        "last_learned_turn": 22,
        "source_turns": [2, 22],
        "canonical_compaction": True,
    }

    full, omitted = _working_records([record], current_turn=40)
    assert [row["fact_id"] for row in full] == ["cmp"]
    assert omitted == []

    older = {"fact_id": "older", "learned_turn": 15, "fact": "Другая запись."}
    assert _tail([record, older], 1)[0]["fact_id"] == "cmp"


def test_legacy_knowledge_without_confidence_does_not_gain_fake_certainty():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].append({
            "fact_id": "legacy",
            "character_id": "npc",
            "learned_turn": 4,
            "fact": "Старая запись без confidence.",
        })

        memory, _, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["legacy"],
                    "summary": "Legacy-запись остаётся без искусственного повышения уверенности после compaction.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )

        current = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]
        assert "confidence" not in current
        assert "source_confidences" not in current


def test_complete_knowledge_records_restores_raw_facts_hidden_by_old_compaction():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].extend([
            {"fact_id": "a", "character_id": "npc", "learned_turn": 2, "fact": "Первый точный факт."},
            {"fact_id": "b", "character_id": "npc", "learned_turn": 7, "fact": "Второй отдельный факт."},
        ])

        memory, _, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["a", "b"],
                    "summary": "Сжатая версия двух фактов.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )

        active = active_memory_records(memory["characters"]["npc"]["knowledge"])
        assert len(active) == 1
        assert active[0]["canonical_compaction"] is True

        complete = complete_knowledge_records(memory["characters"]["npc"]["knowledge"])
        assert [row["fact_id"] for row in complete] == ["a", "b"]
        assert [row["fact"] for row in complete] == [
            "Первый точный факт.",
            "Второй отдельный факт.",
        ]



def test_transport_keeps_independent_first_day_facts_when_old_summary_omitted_secret():
    from app.scene_compaction_runtime import transport_knowledge_records
    from app.turn_context import _working_memory_bucket
    secret = "В первый день POV узнал, что ключ находится под лестницей."
    promise = "В первый день POV пообещал никому не рассказывать о ключе."
    records = [
        {"fact_id": "day1-secret", "learned_turn": 1, "fact": secret,
         "superseded_by": "summary", "raw_evidence_preserved": True},
        {"fact_id": "day1-promise", "learned_turn": 1, "fact": promise,
         "superseded_by": "summary", "raw_evidence_preserved": True},
        {"fact_id": "summary", "fact": "POV узнал про ключ.",
         "learned_turn": 1, "canonical_compaction": True,
         "merged_from": ["day1-secret", "day1-promise"]},
    ]
    projected = transport_knowledge_records(records)
    assert {row["fact_id"] for row in projected} == {"day1-secret", "day1-promise"}
    working = _working_memory_bucket(
        {"knowledge": records}, 100, character_id="pov", cards=[],
    )
    assert {row["fact"] for row in working["knowledge"]} == {secret, promise}
    assert all(row["learned_turn"] == 1 for row in working["knowledge"])


def test_transport_uses_proven_cheap_compaction_but_preserves_provenance():
    from app.scene_compaction_runtime import transport_knowledge_records
    repeated = "Рината узнала, где находится вход в убежище. " + "Достоверный факт. " * 25
    records = [
        {"fact_id": f"f{i}", "learned_turn": i, "fact": repeated,
         "confidence": "certain", "superseded_by": "cmp"}
        for i in (1, 7, 14)
    ] + [{
        "fact_id": "cmp", "fact": repeated,
        "learned_turn": 1, "last_learned_turn": 14,
        "confidence": "certain", "canonical_compaction": True,
        "merged_from": ["f1", "f7", "f14"], "source_turns": [1, 7, 14],
    }]
    compact = transport_knowledge_records(records)
    assert len(compact) == 1
    assert compact[0]["fact_id"] == "cmp"
    assert compact[0]["fact"] == repeated
    assert compact[0]["source_turns"] == [1, 7, 14]
    assert compact[0]["merged_from"] == ["f1", "f7", "f14"]


def test_transport_does_not_merge_conflicting_certainty():
    from app.scene_compaction_runtime import transport_knowledge_records
    fact = "Возможный пароль находится в архиве. " + "Сведения требуют проверки. " * 18
    rows = [
        {"fact_id": "rumor", "learned_turn": 1, "fact": fact,
         "confidence": "uncertain", "superseded_by": "cmp"},
        {"fact_id": "certain", "learned_turn": 8, "fact": fact,
         "confidence": "certain", "superseded_by": "cmp"},
        {"fact_id": "cmp", "learned_turn": 1, "fact": fact,
         "canonical_compaction": True, "merged_from": ["rumor", "certain"],
         "confidence": "mixed"},
    ]
    assert {r["fact_id"] for r in transport_knowledge_records(rows)} == {"rumor", "certain"}


def test_other_npc_does_not_inherit_pov_day_one_secret():
    from app.turn_context import _selected_memory
    from app import storage
    memory = {
        "characters": {
            "pov": {"knowledge": [
                {"fact_id": "pov-secret", "learned_turn": 1, "fact": "POV знает тайный пароль."}
            ]},
            "npc": {"knowledge": [
                {"fact_id": "npc-fact", "learned_turn": 45, "fact": "NPC видит закрытую дверь."}
            ]},
        }
    }
    selected = _selected_memory(storage._normalise_memory(memory), ["pov", "npc"], 100, [])
    assert any("пароль" in r["fact"] for r in selected["pov"]["knowledge"])
    assert not any("пароль" in r["fact"] for r in selected["npc"]["knowledge"])



def test_v5_day_one_journal_compacts_without_losing_facts_or_date():
    from app.scene_compaction_runtime import transport_knowledge_journal
    from app.profile_templates import render_knowledge_journal
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "pov")
        first = "На первом дне POV увидел надпись с координатами. " + "Координаты сохранились. " * 9
        second = first + "На обороте надписи была печать, которую POV тоже заметил."
        bucket["knowledge_journal"] = [
            {"entry_id": "day1-a", "date": "01.09.1206", "period": "день",
             "turn": 1, "text": first},
            {"entry_id": "day1-b", "date": "01.09.1206", "period": "день",
             "turn": 2, "text": second},
        ]
        compacted, _, _, _ = apply_audit_compactions(
            root, {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "pov", "memory_type": "knowledge_journal",
                    "source_ids": ["day1-a", "day1-b"], "summary": second,
                }],
            },
            start_turn=1, end_turn=15, memory=memory, chronology=[],
        )
        persisted = compacted["characters"]["pov"]["knowledge_journal"]
        assert {r["entry_id"] for r in persisted if r["entry_id"] in {"day1-a", "day1-b"}} == {"day1-a", "day1-b"}
        working = transport_knowledge_journal(persisted)
        assert len(working) == 1
        assert working[0]["text"] == second
        assert working[0]["merged_from"] == ["day1-a", "day1-b"]
        assert working[0]["source_turns"] == [1, 2]
        rendered = render_knowledge_journal(working)
        assert "01.09.1206" in rendered and "печать" in rendered
        assert rendered.count(first) == 1


def test_v5_journal_summary_cannot_hide_day_one_secret_or_shift_date():
    from app.scene_compaction_runtime import transport_knowledge_journal
    from app.profile_templates import render_knowledge_journal
    raw = [
        {"entry_id": "secret", "date": "01.09.1206", "period": "ночь",
         "turn": 1, "text": "В первый день POV узнал код 8761.", "superseded_by": "cmp"},
        {"entry_id": "promise", "date": "01.09.1206", "period": "ночь",
         "turn": 1, "text": "POV пообещал не выдавать код.", "superseded_by": "cmp"},
        {"entry_id": "cmp", "date": "01.09.1206", "period": "ночь",
         "turn": 1, "text": "POV узнал некий код.", "canonical_compaction": True,
         "merged_from": ["secret", "promise"]},
    ]
    working = transport_knowledge_journal(raw)
    assert {r["entry_id"] for r in working} == {"secret", "promise"}
    text = render_knowledge_journal(working)
    assert "8761" in text and "не выдавать" in text and "01.09.1206" in text
    wrong_date = [dict(item) for item in raw]
    wrong_date[-1]["text"] = raw[0]["text"] + " " + raw[1]["text"]
    wrong_date[-1]["date"] = "02.09.1206"
    assert {r["entry_id"] for r in transport_knowledge_journal(wrong_date)} == {"secret", "promise"}


def test_v5_journal_compaction_rejects_cross_date_source_facts():
    from app.scene_compaction_runtime import _apply_memory_compactions
    import pytest
    memory = {"characters": {"pov": {"knowledge_journal": [
        {"entry_id": "a", "date": "01.09.1206", "period": "день", "text": "Факт первого дня.", "turn": 1},
        {"entry_id": "b", "date": "02.09.1206", "period": "день", "text": "Факт второго дня.", "turn": 2},
    ]}}}
    with pytest.raises(RuntimeError, match="MEMORY_COMPACTION_CROSS_DATE"):
        _apply_memory_compactions(memory, [{
            "character_id": "pov", "memory_type": "knowledge_journal",
            "source_ids": ["a", "b"], "summary": "Факт первого дня. Факт второго дня.",
        }], start_turn=1, end_turn=15)



def test_transport_legacy_journal_formats_and_canonical_only_fallback():
    from app.scene_compaction_runtime import transport_knowledge_journal
    from app.profile_templates import render_knowledge_journal
    records = [
        "Старинная строка: первый день, пароль 1234.",
        {"entry_id": "compact-only", "canonical_compaction": True,
         "merged_from": ["missing-raw-1", "missing-raw-2"],
         "date": "01.09.1206", "turn": 1,
         "text": "POV сохранил важное обещание с первого дня."},
        {"entry_id": "normal", "date": "02.09.1206", "turn": 2,
         "text": "Обычная старая запись."},
    ]
    projected = transport_knowledge_journal(records)
    assert len(projected) == 3
    rendered = render_knowledge_journal(projected)
    assert "пароль 1234" in rendered
    assert "важное обещание" in rendered
    assert "02.09.1206" in rendered


def test_compact_journal_fallback_keeps_missing_fact_and_surviving_raw():
    from app.scene_compaction_runtime import transport_knowledge_journal
    records = [
        {"entry_id": "known", "turn": 1, "date": "01.09.1206",
         "text": "POV узнал номер архива.", "superseded_by": "cmp"},
        {"entry_id": "cmp", "turn": 1, "date": "01.09.1206",
         "text": "POV также узнал секретный вход.",
         "canonical_compaction": True, "merged_from": ["known", "absent"]},
    ]
    projected = transport_knowledge_journal(records)
    assert {x["entry_id"] for x in projected} == {"known", "cmp"}


def test_proven_compaction_reduces_actual_writer_memory_payload_size():
    import json
    from app.scene_compaction_runtime import transport_knowledge_records
    from app.turn_context import _working_memory_bucket
    fact = "На первом дне Рината получила код секретного архива. " + "Код подтвержден. " * 20
    records = [
        {"fact_id": f"repeat-{i}", "learned_turn": i,
         "fact": fact, "confidence": "certain", "superseded_by": "cmp"}
        for i in range(1, 21)
    ] + [{
        "fact_id": "cmp", "fact": fact, "canonical_compaction": True,
        "confidence": "certain", "learned_turn": 1,
        "source_turns": list(range(1, 21)),
        "merged_from": [f"repeat-{i}" for i in range(1, 21)],
    }]
    projected = transport_knowledge_records(records)
    original_size = len(json.dumps(records[:-1], ensure_ascii=False))
    packet = _working_memory_bucket({"knowledge": records}, 100, character_id="pov", cards=[])
    compressed_size = len(json.dumps(packet["knowledge"], ensure_ascii=False))
    assert len(projected) == len(packet["knowledge"]) == 1
    assert compressed_size < original_size * 0.4
    assert packet["knowledge"][0]["fact"] == fact
    assert packet["knowledge"][0]["source_turns"] == list(range(1, 21))


def test_continuation_block_uses_raw_first_day_facts_not_lossy_active_summary():
    from app.continuation_runtime import _memory_for_range
    memory = {"characters": {"pov": {
        "knowledge": [
            {"fact_id": "secret", "fact": "На первом ходу POV узнал точный код 8901.",
             "learned_turn": 1, "superseded_by": "knowledge-summary"},
            {"fact_id": "promise", "fact": "POV обещал сохранить код в тайне.",
             "learned_turn": 2, "superseded_by": "knowledge-summary"},
            {"fact_id": "knowledge-summary", "fact": "POV знает код.",
             "learned_turn": 1, "canonical_compaction": True,
             "merged_from": ["secret", "promise"]},
        ],
        "knowledge_journal": [
            {"entry_id": "journal-secret", "text": "В первый день Мира сказала: «Серебро».",
             "date": "01.09.1206", "turn": 1, "superseded_by": "journal-summary"},
            {"entry_id": "journal-promise", "text": "POV обещал не раскрывать имя Миры.",
             "date": "01.09.1206", "turn": 2, "superseded_by": "journal-summary"},
            {"entry_id": "journal-summary", "text": "POV и Мира поговорили.",
             "date": "01.09.1206", "turn": 1, "canonical_compaction": True,
             "merged_from": ["journal-secret", "journal-promise"]},
        ],
    }}}
    first = _memory_for_range(memory, 1, 100)["pov"]
    knowledge = [row["text"] for row in first["knowledge"]]
    journal = [row["text"] for row in first["knowledge_journal"]]
    assert "На первом ходу POV узнал точный код 8901." in knowledge
    assert "POV обещал сохранить код в тайне." in knowledge
    assert "В первый день Мира сказала: «Серебро»." in journal
    assert "POV обещал не раскрывать имя Миры." in journal
    assert all("поговорили" not in item for item in journal)
    assert all("знает код." not in item for item in knowledge)
    assert _memory_for_range(memory, 101, 200) == {}


def test_continuation_block_preserves_string_journal_and_source_turn_boundaries():
    from app.continuation_runtime import _memory_for_range
    memory = {"characters": {"mira": {"knowledge_journal": [
        {"entry_id": "first", "turn": 1, "text": "Первый день, важный факт.",
         "date": "01.09.1206"},
        "Старая строка без времени.",
        {"entry_id": "later", "turn": 110, "text": "Сто десятый ход, новый факт.",
         "date": "04.09.1206"},
    ]}}}
    first = _memory_for_range(memory, 1, 100)["mira"]["knowledge_journal"]
    second = _memory_for_range(memory, 101, 200)["mira"]["knowledge_journal"]
    assert {row["text"] for row in first} == {
        "Первый день, важный факт.", "Старая строка без времени.",
    }
    assert [row["text"] for row in second] == ["Сто десятый ход, новый факт."]
