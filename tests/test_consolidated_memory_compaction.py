import tempfile
from pathlib import Path

from app import storage
from app.character_chunk_read import _tail
from app.scene_compaction_runtime import active_memory_records, apply_audit_compactions
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
