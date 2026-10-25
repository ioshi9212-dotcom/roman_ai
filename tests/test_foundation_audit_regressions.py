"""Regression coverage for canon immutability, relationship backstory and audit recency."""
import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app import relationship_file_runtime
from app.scene_compaction_runtime import SCENE_MEMORY_FILE
from app.writer_first_runtime import _rolling_turn_context


def _storage(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "version": 5,
        "novel_id": "verbatim-runtime-regression",
        "title": "Проба",
        "novel": {
            "pov_character": "rina",
            "story_rules": "Если игрок не дал реплику, не придумывай её.\n\nОн охуенно грубый,  не смягчай.",
        },
        "characters": [
            {"character_id": "rina", "name": "Рина", "is_pov": True},
            {"character_id": "dante", "name": "Дантэ", "relationships": [{
                "target_character_id": "rina",
                "relationship_type": "старые знакомые",
                "relationship_context": "Друзья много лет. Он охуенно язвительный,  но умеет держать слово.",
                "current_dynamic": "Они постоянно подначивают друг друга.",
                "dimensions": [{"label": "интерес", "value": 19}],
            }]},
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "комната", "present_characters": ["rina", "dante"]},
        },
    }


def _read_context(sid):
    manifest = session_runtime.prepare_turn_packet(sid, "")
    chunks = [manifest["content"]]
    for idx in range(1, manifest["chunk_count"]):
        chunks.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], idx)["content"])
    return json.loads("".join(chunks))


def test_prepare_does_not_modify_saved_author_canon_on_disk():
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        source_path = root / "source.json"
        before = source_path.read_bytes()
        original = storage._read_json(source_path, {})
        assert "  не смягчай" in str(original)
        _read_context(sid)
        after = source_path.read_bytes()
        assert after == before
        # Even the discontinued POV-silence instruction is preserved in RAW.
        assert "Если игрок не дал реплику" in after.decode("utf-8")


def test_card_writer_view_keeps_complete_author_relationship_prose_but_not_stale_numbers():
    source = _novel()["characters"][1]
    full = source["relationships"][0]["relationship_context"]
    writer = relationship_file_runtime.card_for_context(source)
    assert "relationships" not in writer
    entry = writer["relationship_background_director_only"]["entries"][0]
    assert entry["relationship_context"] == full
    assert "current_dynamic" not in entry  # current attitude must come from the live file
    assert "dimensions" not in entry
    assert source["relationships"][0]["dimensions"][0]["value"] == 19


def test_active_scene_writer_packet_receives_original_relationship_background():
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        context = _read_context(sid)
        cards = {c["character_id"]: c for c in context["character_cards"]}
        assert "Друзья много лет." in json.dumps(
            cards["dante"]["relationship_background_director_only"], ensure_ascii=False
        )
        assert cards["dante"]["relationship_background_director_only"]["entries"][0]["relationship_context"].endswith(
            "  но умеет держать слово."
        )


def test_last_two_full_dialogues_survive_scene_compaction():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = [{"turn_number": n, "user_input": f"ход {n}",
                 "scene_output": f"Диалог {n}: сохрани обещание."} for n in range(1, 16)]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        (root / SCENE_MEMORY_FILE).write_text(json.dumps({
            "scenes": [{"scene_id": "scene_1", "source_ranges": [[1, 15]],
                        "summary": "Краткая сводка, недостаточная для живого диалога."}]
        }, ensure_ascii=False), encoding="utf-8")
        recent, continuity = _rolling_turn_context(root)
        assert [r["turn_number"] for r in recent] == [14, 15]
        assert "сохрани обещание" in recent[-1]["scene_output"]
        assert continuity == []
