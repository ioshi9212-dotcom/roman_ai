"""Repeated testimony must preserve who learned what and WHEN."""
import tempfile
from pathlib import Path

from app import knowledge_persistence_runtime, storage


def _session(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()
    sid = storage.create_session({
        "novel_id": "test_journal_event_provenance",
        "title": "Knowledge source attribution",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "silas", "name": "Сайлас"},
        ],
        "starting_state": {
            "current": {"location": "room", "present_characters": ["pov", "silas"]},
            "pov": {"character_id": "pov"},
        },
    })["session_id"]
    return sid, storage.SESSIONS_DIR / sid


def test_repeated_same_words_different_turn_or_speaker_are_not_erased():
    with tempfile.TemporaryDirectory() as tmp:
        sid, root = _session(tmp)
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        base = {"date": "24.09.2026", "period": "вечер", "text": "Рината сказала: Я вернусь.",
                "speaker_id": "pov"}
        bucket["knowledge_journal"] = [
            {"entry_id": "first", "turn": 3, **base},
            {"entry_id": "repeated", "turn": 36, **base},
            {"entry_id": "same_message_duplicate", "turn": 36, **base},
            {"entry_id": "other_source", "turn": 36, **{**base, "speaker_id": "dante"}},
        ]
        storage._write_json(root / "memory.json", memory)
        count = knowledge_persistence_runtime.dedupe_persisted_knowledge_journal(sid)
        assert count == 1
        saved = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        events = saved["characters"]["silas"]["knowledge_journal"]
        assert [row["entry_id"] for row in events] == ["first", "repeated", "other_source"]
        assert [row["turn"] for row in events] == [3, 36, 36]


def test_new_statement_does_not_drop_distinct_later_observation():
    with tempfile.TemporaryDirectory() as tmp:
        sid, root = _session(tmp)
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge_journal"] = [
            {"entry_id": "earlier", "turn": 5, "date": "01.09.2026",
             "period": "утро", "text": "Я вернусь.", "speaker_id": "pov"},
        ]
        storage._write_json(root / "memory.json", memory)
        proposed = {
            "extracted": {"knowledge_journal_add": [
                {"character_id": "silas", "date": "29.10.2026", "period": "вечер",
                 "text": "Я вернусь.", "speaker_id": "pov"},
                {"character_id": "silas", "date": "29.10.2026", "period": "вечер",
                 "text": "Я вернусь.", "speaker_id": "pov"},
            ]},
        }
        result = knowledge_persistence_runtime.dedupe_new_journal_against_persisted(sid, proposed)
        assert len(result["extracted"]["knowledge_journal_add"]) == 1
        assert len(proposed["extracted"]["knowledge_journal_add"]) == 2
