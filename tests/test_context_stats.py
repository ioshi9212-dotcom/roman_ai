import json
import tempfile
import time
from pathlib import Path

from app import session_runtime, storage
from app.context_stats import session_context_stats


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_context_stats_is_read_only_and_reports_memory_chronology_and_packet_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "diag",
            "title": "Diagnostics",
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {
                    "date": "02.09.2026",
                    "time": "10:00",
                    "location": "room",
                    "present_characters": ["pov", "npc"],
                },
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._read_json(root / "memory.json", {})
        memory.setdefault("characters", {})["npc"] = {
            "knowledge": [{"fact_id": "f1", "fact": "x"}],
            "experiences": [{"event_id": "e1", "event": "y"}],
            "dialogue_memory": [{"topic_id": "t1", "summary": "z"}],
        }
        storage._write_json(root / "memory.json", memory)
        storage._write_json(root / "chronology.json", [
            {"event_id": "c1", "turn_number": 1, "event": "event one", "importance": "major"},
            {"event_id": "c2", "turn_number": 2, "event": "event two", "importance": "normal"},
        ])
        repeated = {"blob": "x" * 800}
        payload = {
            "author_context": {"memory_full": repeated, "memory_copy": repeated},
            "scene_state": {"location": "room"},
            "runtime_rules": "rules",
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        storage._write_json(root / "turn_packet.json", {
            "packet_id": "diag-packet",
            "chunks": [raw[:500], raw[500:]],
            "read_chunks": [],
        })

        before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        stats = session_context_stats(sid)
        after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}

        assert stats["read_only"] is True
        assert stats["chronology"]["events"] == 2
        assert stats["chronology"]["importance_counts"] == {"major": 1, "normal": 1}
        assert stats["memory"]["by_character"]["npc"]["knowledge"] == 1
        assert stats["memory"]["by_character"]["npc"]["experiences"] == 1
        assert stats["memory"]["by_character"]["npc"]["dialogue_memory"] == 1
        packet = stats["turn_packet"]
        assert packet["present"] is True
        assert packet["chunk_count"] == 2
        assert packet["top_level_chars"]["author_context"] > packet["top_level_chars"]["scene_state"]
        assert packet["nested_dict_chars"]["author_context"]["memory_full"] > 500
        duplicate_paths = [row["paths"] for row in packet["exact_duplicate_blocks"]]
        assert ["author_context.memory_full", "author_context.memory_copy"] in duplicate_paths
        assert before == after

def _turn_zero_perf_novel(cast_count: int):
    characters = [
        {
            "character_id": "pov",
            "name": "POV",
            "is_pov": True,
            "story_function": "главная героиня",
            "appearance": "Короткое описание внешности POV. " + "A" * 700,
            "character": "Характер POV. " + "B" * 900,
            "speech": "Манера речи POV. " + "C" * 350,
            "habits": ["привычка 1", "привычка 2"],
            "goals": ["цель POV"],
            "background": "Предыстория POV. " + "D" * 900,
        }
    ]
    for index in range(cast_count - 1):
        cid = f"npc_{index}"
        target = f"npc_{(index + 1) % max(1, cast_count - 1)}"
        relation = []
        if cast_count > 2 and target != cid:
            relation = [{
                "target_character_id": target,
                "relationship_type": "давно знакомы",
                "relationship_context": "Устойчивая связь между персонажами.",
                "current_dynamic": "Есть собственная динамика и незакрытый вопрос.",
            }]
        characters.append({
            "character_id": cid,
            "name": f"NPC {index}",
            "story_function": f"постоянный персонаж {index}",
            "appearance": "Внешность. " + "E" * 700,
            "character": "Характер. " + "F" * 900,
            "speech": "Манера речи. " + "G" * 350,
            "habits": ["привычка 1", "привычка 2"],
            "work": f"работа {index}",
            "residence": f"место {index}",
            "goals": [f"цель {index}", "вторая цель"],
            "background": "Предыстория. " + "H" * 900,
            "relationships": relation or None,
        })
    return {
        "novel_id": f"turn-zero-perf-{cast_count}",
        "title": "Turn Zero Perf",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {
            "pov_character": "pov",
            "genres": ["romance"],
            "premise": "Стартовая сцена без накопленной истории.",
        },
        "lore": {"setting": "Короткий стартовый лор."},
        "rules": {},
        "hidden_lore": {"entries": ["Скрытый стартовый факт."]},
        "world": {"setting": "мир"},
        "characters": characters,
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "02.09.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["pov", "npc_0"] if cast_count > 1 else ["pov"],
            },
        },
    }


def test_turn_zero_prepare_diagnostic(capsys):
    rows = []
    for cast_count in (2, 10, 30):
        with tempfile.TemporaryDirectory() as tmp:
            setup_temp_storage(tmp)
            sid = storage.create_session(_turn_zero_perf_novel(cast_count))["session_id"]

            started = time.perf_counter()
            manifest = session_runtime.prepare_turn_packet(sid, "")
            prepare_ms = round((time.perf_counter() - started) * 1000, 2)

            stats = session_context_stats(sid)
            packet = stats["turn_packet"]
            root = storage.SESSIONS_DIR / sid
            saved = storage._read_json(root / "turn_packet.json", {})
            raw = "".join(saved.get("chunks", []))
            context = json.loads(raw)

            semantic_duplicate_sizes = {
                "character_cards": len(json.dumps(context.get("character_cards"), ensure_ascii=False, separators=(",", ":"))),
                "character_profiles": len(json.dumps(context.get("character_profiles"), ensure_ascii=False, separators=(",", ":"))),
                "character_registry": len(json.dumps(context.get("character_registry"), ensure_ascii=False, separators=(",", ":"))),
                "cast_registry": len(json.dumps(context.get("cast_registry"), ensure_ascii=False, separators=(",", ":"))),
                "scene_characters": len(json.dumps(context.get("scene_characters"), ensure_ascii=False, separators=(",", ":"))),
                "starting_state": len(json.dumps(context.get("starting_state"), ensure_ascii=False, separators=(",", ":"))),
            }

            unread = [
                index for index in range(1, int(manifest.get("chunk_count", 0) or 0))
                if index not in set(saved.get("read_chunks", []))
            ]
            read_started = time.perf_counter()
            for index in unread:
                storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
            chunk_read_ms = round((time.perf_counter() - read_started) * 1000, 2)

            rows.append({
                "cast_count": cast_count,
                "prepare_ms": prepare_ms,
                "payload_chars": packet["payload_chars"],
                "packet_file_chars": packet["file_chars"],
                "chunk_count": packet["chunk_count"],
                "remaining_chunk_actions": max(0, packet["chunk_count"] - 1),
                "local_chunk_read_ms": chunk_read_ms,
                "approx_full_packet_chars_rewritten_while_reading": max(0, packet["chunk_count"] - 1) * packet["file_chars"],
                "largest_top_level_blocks": list(packet["top_level_chars"].items())[:15],
                "semantic_duplicate_sizes": semantic_duplicate_sizes,
                "exact_duplicate_blocks": packet["exact_duplicate_blocks"][:10],
            })

    print("TURN_ZERO_PERF_DIAGNOSTIC=" + json.dumps(rows, ensure_ascii=False))
    captured = capsys.readouterr()
    assert "TURN_ZERO_PERF_DIAGNOSTIC=" in captured.out

