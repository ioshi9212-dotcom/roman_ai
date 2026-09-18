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
        "novel_id": "memory-compaction-semantics",
        "title": "Memory Compaction Semantics",
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
            f"На ходах {start_turn}–{end_turn} POV и NPC продолжали одну сцену, "
            "обменивались важными репликами и фактами, а диапазон завершился в зафиксированной точке продолжения."
        ),
        "participants": ["pov", "npc"],
        "location": "room",
        "status": "closed",
    }


def test_knowledge_compaction_preserves_first_and_last_learned_turn_and_does_not_promote_confidence():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge"].extend([
            {
                "fact_id": "old-a",
                "character_id": "npc",
                "learned_turn": 2,
                "fact": "NPC слышал неподтверждённый слух.",
                "confidence": "uncertain",
            },
            {
                "fact_id": "old-b",
                "character_id": "npc",
                "learned_turn": 7,
                "fact": "NPC позже получил часть подтверждения.",
                "confidence": "certain",
            },
        ])

        memory, chronology, store, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["old-a", "old-b"],
                    "summary": "NPC знает сообщение, но часть его содержания всё ещё остаётся неподтверждённой.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )

        active = active_memory_records(memory["characters"]["npc"]["knowledge"])
        assert len(active) == 1
        current = active[0]
        assert current["learned_turn"] == 2
        assert current["first_learned_turn"] == 2
        assert current["last_learned_turn"] == 7
        assert current["confidence"] == "uncertain"
        assert current["source_confidences"] == [
            {"source_id": "old-a", "confidence": "uncertain"},
            {"source_id": "old-b", "confidence": "certain"},
        ]

        raw = {row["fact_id"]: row for row in memory["characters"]["npc"]["knowledge"] if row["fact_id"] in {"old-a", "old-b"}}
        assert raw["old-a"]["confidence"] == "uncertain"
        assert raw["old-b"]["confidence"] == "certain"


def test_rolling_compaction_flattens_confidence_provenance_and_preserves_strongest_durability():
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

        memory, chronology, store, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["a", "b"],
                    "summary": "NPC объединяет первый и второй фрагмент знания, сохраняя исходную степень уверенности.",
                }],
            },
            start_turn=1,
            end_turn=15,
            memory=memory,
            chronology=[],
        )
        first = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]

        memory["characters"]["npc"]["knowledge"].append({
            "fact_id": "c",
            "character_id": "npc",
            "learned_turn": 22,
            "fact": "Новый противоречивый слух.",
            "confidence": "rumor",
            "importance": "critical",
            "permanent": True,
        })

        memory, chronology, store, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(16, 30)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": [first["fact_id"], "c"],
                    "summary": "NPC хранит объединённую версию, в которой подтверждённые детали соседствуют с противоречивыми слухами.",
                }],
            },
            start_turn=16,
            end_turn=30,
            memory=memory,
            chronology=chronology,
        )

        current = active_memory_records(memory["characters"]["npc"]["knowledge"])[0]
        assert current["learned_turn"] == 2
        assert current["first_learned_turn"] == 2
        assert current["last_learned_turn"] == 22
        assert set(current["source_turns"]) == {2, 7, 22}
        assert set(current["merged_from"]) == {"a", "b", "c"}

        assert current["confidence"] == "mixed"
        assert current["source_confidences"] == [
            {"source_id": "a", "confidence": "uncertain"},
            {"source_id": "b", "confidence": "certain"},
            {"source_id": "c", "confidence": "rumor"},
        ]

        assert current["importance"] == "critical"
        assert current["source_importance"] == ["anchor", "critical"]
        assert current["durable"] is True
        assert current["pinned"] is True
        assert current["permanent"] is True


def test_fresh_detail_keeps_old_canonical_knowledge_in_recent_working_memory():
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

    # Offscreen participation uses the same recency semantics when choosing its tail.
    older = {"fact_id": "older", "learned_turn": 15, "fact": "Другая запись."}
    assert _tail([record, older], 1)[0]["fact_id"] == "cmp"


def test_compaction_without_explicit_confidence_does_not_invent_certain():
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
            "fact": "Старая legacy-запись без confidence.",
        })

        memory, _, _, _ = apply_audit_compactions(
            root,
            {
                "scene_compactions": [scene(1, 15)],
                "memory_compactions": [{
                    "character_id": "npc",
                    "memory_type": "knowledge",
                    "source_ids": ["legacy"],
                    "summary": "Legacy-запись сохраняется без искусственного повышения уверенности после compaction.",
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
