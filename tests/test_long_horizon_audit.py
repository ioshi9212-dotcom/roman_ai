import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.long_horizon_audit import (
    apply_macro_chronology_compaction,
    build_macro_payload,
)


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _turn(number: int, date: str):
    return {
        "turn_number": number,
        "scene_output": (
            f"🎭 Тест · осень\n"
            f"🕒 День 1 · четверг, {date}, 10:00 · 📍 дом\n"
            f"Сцена {number}"
        ),
        "extracted": {},
    }


def test_macro_audit_payload_is_added_only_on_each_60th_turn_for_v5():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        source = {"version": 5, "profile_schema": {"version": 1}}
        state = {"world": {"cast_registry": {}}}
        turns = [_turn(i, "24.09.2026" if i <= 30 else "25.09.2026") for i in range(1, 61)]

        payload = build_macro_payload(
            root,
            source=source,
            state=state,
            chronology=[],
            turns=turns,
            end_turn=60,
        )
        assert payload is not None
        assert payload["required"] is True
        assert payload["macro_range"] == [1, 60]
        assert payload["turn_calendar"] == [
            {"date": "24.09.2026", "start_turn": 1, "end_turn": 30},
            {"date": "25.09.2026", "start_turn": 31, "end_turn": 60},
        ]

        assert build_macro_payload(
            root,
            source=source,
            state=state,
            chronology=[],
            turns=turns[:45],
            end_turn=45,
        ) is None


def test_macro_compaction_replaces_raw_60_turn_chronology_with_dated_paragraphs():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = "sid"
        root = storage.SESSIONS_DIR / sid
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        storage._write_json(root / "meta.json", {"session_id": sid, "turn_number": 60})

        turns = [_turn(i, "24.09.2026" if i <= 30 else "25.09.2026") for i in range(1, 61)]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in turns),
            encoding="utf-8",
        )

        chronology = [
            {
                "event_id": "routine",
                "turn_number": 3,
                "story_date": "24.09.2026",
                "event": "Рината поела.",
                "importance": "normal",
            },
            {
                "event_id": "major",
                "turn_number": 20,
                "story_date": "24.09.2026",
                "event": "Рината впервые увидела, как Сайлас проходит через отражение.",
                "importance": "major",
            },
            {
                "event_id": "meeting",
                "turn_number": 45,
                "story_date": "25.09.2026",
                "event": "Они договорились встретиться ровно в 18:30.",
                "importance": "major",
                "time_critical": True,
                "exact_time": "18:30",
            },
        ]
        repairs = {
            "chronology_compactions": [
                {
                    "date": "24.09.2026",
                    "summary": "Рината впервые увидела сверхъестественный переход Сайласа через отражение и поняла, что он скрывает природу своих возможностей.",
                    "importance": "major",
                    "participants": ["rinata", "silas"],
                },
                {
                    "date": "25.09.2026",
                    "summary": "Рината и Сайлас договорились о следующей встрече; точное время осталось важным условием договорённости.",
                    "importance": "major",
                    "participants": ["rinata", "silas"],
                },
            ]
        }

        result = apply_macro_chronology_compaction(
            root,
            chronology,
            repairs,
            end_turn=60,
        )

        assert len(result) == 2
        assert all(row["canonical_macro_compaction"] is True for row in result)
        assert not any("поела" in row["event"] for row in result)
        second = next(row for row in result if row["story_date"] == "25.09.2026")
        assert second["critical_times"] == ["18:30"]
        assert second["source_turn_range"] == [31, 60]


def test_macro_compaction_requires_output_on_v5_turn_60():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        (root / "turns.jsonl").write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError, match="MACRO_CHRONOLOGY_COMPACTION_REQUIRED"):
            apply_macro_chronology_compaction(
                root,
                [],
                {},
                end_turn=60,
            )


def test_legacy_v4_audit_does_not_require_macro_compaction():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 4})
        result = apply_macro_chronology_compaction(root, [], {}, end_turn=60)
        assert result == []
