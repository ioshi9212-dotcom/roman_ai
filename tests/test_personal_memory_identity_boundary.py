import json
import tempfile
from pathlib import Path

from app import session_migrations, session_runtime, storage, turn_context


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "identity-memory-boundary",
        "title": "Identity Memory Boundary",
        "novel": {"pov_character": "kair_venner"},
        "characters": [
            {"character_id": "kair_venner", "name": "Кайр", "is_pov": True},
            {"character_id": "mira_veil", "name": "Мира"},
        ],
        "starting_state": {
            "pov": {"character_id": "kair_venner"},
            "current": {
                "date": "06.10.2026",
                "time": "12:00",
                "location": "cafe",
                "present_characters": ["kair_venner", "mira_veil"],
            },
        },
    }


def read_all(manifest, session_id: str):
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def test_physical_dialogue_cannot_stay_remote_only_because_model_marked_it_remote():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(ответить Мире)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(ответить Мире)",
                "scene_output": "**Кайр** — Привет.\n**Мира** — Привет.",
                "extracted": {
                    "dialogue_memory_add": [{
                        "topic_id": "wrong_remote",
                        "participants": ["kair_venner", "mira_veil"],
                        "mode": "remote",
                        "summary": "Кайр: Привет. Мира: Привет.",
                        "segments": [
                            {"speaker_id": "kair_venner", "text": "Привет."},
                            {"speaker_id": "mira_veil", "text": "Привет."},
                        ],
                    }],
                },
            },
        )

        root = storage.SESSIONS_DIR / sid
        turns = storage._read_turns(root)
        saved = turns[-1]["extracted"]["dialogue_memory_add"][0]
        assert saved["mode"] == "physical"

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        mira_journal = " ".join(
            str(row.get("text") or "")
            for row in memory["characters"]["mira_veil"]["knowledge_journal"]
            if isinstance(row, dict)
        )
        assert "Удалённая коммуникация" not in mira_journal

        next_manifest = session_runtime.prepare_turn_packet(sid, "(остаться рядом)")
        context = read_all(next_manifest, sid)
        mira_memory = context["character_memory"]["mira_veil"]
        blob = json.dumps(mira_memory, ensure_ascii=False)
        assert "kair_venner" not in blob
        assert "Кайр:" not in blob
        assert "Привет." in blob


def test_real_remote_memory_keeps_content_but_not_unknown_counterpart_identity():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        source = novel()
        source["starting_state"]["current"]["present_characters"] = ["kair_venner"]
        source["starting_state"]["current"]["remote_characters"] = ["mira_veil"]
        sid = storage.create_session(source)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(ответить сообщением)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(ответить сообщением)",
                "scene_output": "**Кайр (в сообщениях Мире)** — Не бойся.",
                "extracted": {},
            },
        )

        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        mira_journal = " ".join(
            str(row.get("text") or "")
            for row in memory["characters"]["mira_veil"]["knowledge_journal"]
            if isinstance(row, dict)
        )
        assert "Не бойся." in mira_journal
        assert "Кайр" not in mira_journal
        assert "kair_venner" not in mira_journal

        next_manifest = session_runtime.prepare_turn_packet(sid, "(ждать)")
        context = read_all(next_manifest, sid)
        mira_memory = context["character_memory"].get("mira_veil", {})
        blob = json.dumps(mira_memory, ensure_ascii=False)
        assert "Не бойся." in blob
        assert "kair_venner" not in blob
        assert "Кайр:" not in blob
        assert "participants" not in blob
        assert "speaker_id" not in blob


def test_v2_migration_neutralizes_old_generated_remote_identity_labels():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta["data_schema_version"] = 1
        storage._write_json(root / "meta.json", meta)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "mira_veil")
        bucket["knowledge_journal"] = [{
            "entry_id": "legacy_remote",
            "turn": 6,
            "text": "Удалённая коммуникация с Кайр: Кайр: Не бойся. Мира: Я и не боюсь.",
        }]
        bucket["dialogue_memory"] = [{
            "topic_id": "remote_t6_kair_venner",
            "turn": 6,
            "participants": ["kair_venner", "mira_veil"],
            "mode": "remote",
            "summary": "Кайр: Не бойся. Мира: Я и не боюсь.",
            "segments": [
                {"speaker_id": "kair_venner", "text": "Не бойся."},
                {"speaker_id": "mira_veil", "text": "Я и не боюсь."},
            ],
        }]
        storage._write_json(root / "memory.json", memory)

        result = session_migrations.ensure_current_session_data(sid)
        assert result["data_schema_version"] == 2

        migrated = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        text = migrated["characters"]["mira_veil"]["knowledge_journal"][0]["text"]
        assert text.startswith("Коммуникация:")
        assert "Кайр" not in text
        assert "kair_venner" not in text
        assert "Собеседник:" in text

        cards = storage._load_cards(root, storage._read_json(root / "source.json", {}))
        safe = turn_context._working_memory_bucket(
            migrated["characters"]["mira_veil"],
            20,
            character_id="mira_veil",
            cards=cards,
        )
        blob = json.dumps(safe, ensure_ascii=False)
        assert "Кайр:" not in blob
        assert "kair_venner" not in blob
        assert "remote_t6_kair_venner" not in blob
        assert "participants" not in blob
        assert "speaker_id" not in blob
