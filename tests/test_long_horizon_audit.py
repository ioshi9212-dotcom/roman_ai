import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import main, storage
from app.long_horizon_audit import (
    apply_macro_chronology_compaction,
    build_macro_payload,
    MacroChronologyMissingDates,
    replay_macro_chronology_compaction,
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


def test_missing_macro_compaction_is_rejected_on_v5_turn_60():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        (root / "turns.jsonl").write_text("", encoding="utf-8")
        chronology = [
            {
                "event_id": "keep-me",
                "turn_number": 20,
                "story_date": "24.09.2026",
                "event": "Важный факт остаётся в сырой chronology.",
                "importance": "major",
            }
        ]

        with pytest.raises(RuntimeError, match="MACRO_CHRONOLOGY_COMPACTION_REQUIRED"):
            apply_macro_chronology_compaction(
                root,
                chronology,
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


def test_replay_keeps_historical_v5_audit_that_predates_macro_compaction():
    chronology = [{"event_id": "legacy", "turn_number": 60, "event": "Старый важный факт."}]
    result = replay_macro_chronology_compaction(
        {"version": 5, "profile_schema": {"version": 1}},
        [],
        chronology,
        {"scene_compactions": []},
        end_turn=60,
    )
    assert result == chronology


def test_second_macro_boundary_at_120_also_requires_compaction():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        turns = [_turn(i, "25.09.2026" if i <= 60 else "26.09.2026") for i in range(1, 121)]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in turns),
            encoding="utf-8",
        )
        chronology = [
            {
                "event_id": "macro_60_1",
                "turn_number": 60,
                "story_date": "25.09.2026",
                "event": "Старый уже сжатый диапазон 1–60.",
                "importance": "major",
                "canonical_macro_compaction": True,
                "source_turn_range": [1, 60],
            },
            {
                "event_id": "raw-90",
                "turn_number": 90,
                "story_date": "26.09.2026",
                "event": "Важное событие второго шестидесятиходового диапазона.",
                "importance": "major",
            },
        ]

        with pytest.raises(RuntimeError, match="MACRO_CHRONOLOGY_COMPACTION_REQUIRED"):
            apply_macro_chronology_compaction(
                root,
                chronology,
                {},
                end_turn=120,
            )


def test_iso_and_dotted_important_dates_are_one_calendar_day():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        turns = [_turn(i, "09.10.2026") for i in range(1, 61)]
        turns[19]["extracted"] = {"state_patch": {"current": {"date": "2026-10-09"}}}
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in turns),
            encoding="utf-8",
        )
        chronology = [
            {
                "event_id": "event-iso", "turn_number": 20,
                "story_date": "2026-10-09", "importance": "major",
                "event": "Важная сцена, сохранённая с датой в ISO-формате.",
            },
            {
                "event_id": "event-dotted", "turn_number": 25,
                "story_date": "09.10.2026", "importance": "anchor",
                "event": "Второе важное событие того же игрового дня.",
            },
        ]
        original_chronology = json.dumps(chronology, ensure_ascii=False)
        macro = build_macro_payload(
            root,
            source={"version": 5, "profile_schema": {"version": 1}},
            state={"world": {"cast_registry": {}}},
            chronology=chronology,
            turns=turns, end_turn=60,
        )
        assert macro["required_important_dates"] == ["09.10.2026"]
        assert macro["turn_calendar"] == [
            {"date": "09.10.2026", "start_turn": 1, "end_turn": 60}
        ]
        assert {row["date"] for row in macro["chronology_events_before_compaction"]} == {"09.10.2026"}

        compacted = apply_macro_chronology_compaction(root, chronology, {
            "chronology_compactions": [{
                "date": "09.10.2026",
                "summary": "В этот день два важных события были последовательно сохранены без утраты их смысловой связи.",
            }]
        }, end_turn=60)
        assert len(compacted) == 1
        assert compacted[0]["story_date"] == "09.10.2026"
        assert compacted[0]["source_turn_range"] == [1, 60]
        assert json.dumps(chronology, ensure_ascii=False) == original_chronology


def test_macro_missing_date_error_reports_exact_dates_and_never_drops_important_days():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        (root / "turns.jsonl").write_text("", encoding="utf-8")
        chronology = [
            {"event_id": "one", "turn_number": 1, "story_date": "2025-10-02",
             "event": "Первое важное событие.", "importance": "major"},
            {"event_id": "two", "turn_number": 40, "story_date": "2026-04-09",
             "event": "Второе важное событие.", "importance": "anchor"},
            {"event_id": "three", "turn_number": 45, "story_date": "10.04.2026",
             "event": "Третье важное событие.", "importance": "critical"},
            {"event_id": "four", "turn_number": 50, "story_date": "2026-10-09",
             "event": "Четвёртое важное событие.", "importance": "normal"},
        ]
        with pytest.raises(MacroChronologyMissingDates) as exc:
            apply_macro_chronology_compaction(root, chronology, {
                "chronology_compactions": [{
                    "date": "09.04.2026", "summary": "Сохранено важное событие девятого апреля, но другие важные даты не забываются.",
                }],
            }, end_turn=60)
        assert exc.value.missing_dates == ["02.10.2025", "10.04.2026"]
        # The original data remains untouched until a complete audit succeeds.
        assert len(chronology) == 4
        assert chronology[0]["story_date"] == "2025-10-02"


def test_audit_http_409_explains_missing_dates_and_keeps_same_audit_id(monkeypatch):
    def fail(_sid, _payload):
        raise MacroChronologyMissingDates(["02.10.2025", "09.10.2026"])

    monkeypatch.setattr(main, "commit_audit_request", fail)
    from app.models import AuditCommit
    with pytest.raises(HTTPException) as exc:
        main.audit_commit("sid", AuditCommit(
            audit_id="keep-original-audit", start_turn=46, end_turn=60,
            repairs={"scene_compactions": [], "chronology_compactions": []},
        ))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MACRO_CHRONOLOGY_IMPORTANT_DATE_MISSING"
    assert exc.value.detail["missing_dates"] == ["02.10.2025", "09.10.2026"]
    assert "SAME audit_id" in exc.value.detail["instruction"]


def test_iso_summary_date_is_accepted_and_saved_as_dotted_canonical_date():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "sid"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {"version": 5, "profile_schema": {"version": 1}})
        (root / "turns.jsonl").write_text("", encoding="utf-8")
        chronology = [
            {"event_id": "iso-1", "turn_number": 12, "story_date": "09.10.2026",
             "event": "Важное событие в хронологии.", "importance": "anchor"},
        ]
        result = apply_macro_chronology_compaction(
            root, chronology, {"chronology_compactions": [{
                "date": "2026-10-09",
                "summary": "Первое важное событие принято в обоих форматах одной календарной даты.",
            }]}, end_turn=60,
        )
        assert len(result) == 1
        assert result[0]["story_date"] == "09.10.2026"

