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
