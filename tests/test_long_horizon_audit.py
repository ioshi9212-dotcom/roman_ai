import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.long_horizon_audit import (
    apply_macro_chronology_compaction,
    build_macro_payload,
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



def test_offscreen_actor_events_survive_60_and_120_turns_and_writer_retrieval():
    """Real macro compaction must not turn NPC agency into anonymous background."""
    from app import session_runtime, writer_first_runtime
    from app.continuation_runtime import _normalized_chronology

    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "actor-history"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {
            "version": 5, "profile_schema": {"version": 1},
        })
        turns = [_turn(i, "24.09.2026" if i <= 30 else "25.09.2026") for i in range(1, 61)]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in turns),
            encoding="utf-8",
        )
        first_events = [
            {
                "event_id": "daren-investigation",
                "turn_number": 4,
                "story_date": "24.09.2026",
                "event": "Дарен нашёл важный след в заброшенном переходе.",
                "location": "заброшенный переход",
                "actor_character_id": "daren",
                "participants_present": [],
                "importance": "major",
            },
            {
                "event_id": "var-search",
                "turn_number": 8,
                "story_date": "24.09.2026",
                "event": "Вар независимо обнаружил архив с доказательствами.",
                "actor_character_id": "var",
                "participants_present": [],
                "importance": "major",
            },
            {
                "event_id": "pov-decision",
                "turn_number": 39,
                "story_date": "25.09.2026",
                "event": "POV заключил важное соглашение.",
                "participants_present": ["pov"],
                "importance": "major",
            },
        ]
        repairs = {"chronology_compactions": [
            {
                "date": "24.09.2026",
                "summary": "Дарен исследовал заброшенный переход, а Вар нашёл дополнительные доказательства.",
                "importance": "major",
                "participants": [],
            },
            {
                "date": "25.09.2026",
                "summary": "POV заключил важное соглашение, имеющее последствия для остальных.",
                "importance": "major",
                "participants": ["pov"],
            },
        ]}
        after_60 = apply_macro_chronology_compaction(
            root, first_events, repairs, end_turn=60,
        )
        row = next(x for x in after_60 if x["story_date"] == "24.09.2026")
        assert row.get("participants_present", []) == []
        assert {e["actor_character_id"] for e in row["actor_events"]} == {"daren", "var"}
        assert {e["source_event_id"] for e in row["actor_events"]} == {
            "daren-investigation", "var-search",
        }

        # 60 later unrelated turns must not make actor-linked history unfindable.
        later = [{
            "event_id": f"unrelated-{n}",
            "turn_number": n,
            "story_date": "26.09.2026",
            "event": f"Другое событие номер {n}.",
            "importance": "normal",
        } for n in range(61, 121)]
        turns_120 = [*turns, *[_turn(i, "26.09.2026") for i in range(61, 121)]]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in turns_120),
            encoding="utf-8",
        )
        after_120 = apply_macro_chronology_compaction(
            root,
            [*after_60, *later],
            {"chronology_compactions": [{
                "date": "26.09.2026",
                "summary": "За следующие шестьдесят ходов произошли различные события, не изменившие старые расследования.",
                "importance": "normal",
            }]},
            end_turn=120,
        )
        assert any(x["event_id"] == "macro_60_1" for x in after_120)
        full = [
            *after_120,
            *[{
                "event_id": f"later-{turn}", "turn_number": turn,
                "story_date": "27.09.2026", "event": f"Позднее событие {turn}.",
                "importance": "normal",
            } for turn in range(121, 181)],
        ]
        selected = session_runtime._select_chronology_context(
            full, relevant_character_ids=["daren"], location=None,
        )
        assert "macro_60_1" in {e["event_id"] for e in selected}
        compact = writer_first_runtime._compact_chronology(
            full, ["var"], "другое место",
        )
        assert "macro_60_1" in {e["event_id"] for e in compact}

        continuation = _normalized_chronology(
            {"chronology": [{
                "date": "24.09.2026",
                "summary": "Старое расследование позднее повлияло на развитие истории.",
                "importance": "normal",
                "participants": [],
            }]},
            source_chronology=full,
        )
        carried = next(e for e in continuation if e.get("story_date") == "24.09.2026")
        assert {a["actor_character_id"] for a in carried["actor_events"]} == {"daren", "var"}
        assert not carried.get("participants_present")
        # A second continuation must also preserve the same original evidence.
        again = _normalized_chronology(
            {"chronology": [{
                "date": "24.09.2026",
                "summary": "История расследования по-прежнему важна.",
                "importance": "normal",
            }]},
            source_chronology=continuation,
        )
        again_row = next(x for x in again if x.get("story_date") == "24.09.2026")
        assert {a["actor_character_id"] for a in again_row["actor_events"]} == {"daren", "var"}
        assert "actor_character_id" not in again_row
        assert not again_row.get("participants_present")
        # Even if a final summarizer omits the entire date, the backend must
        # preserve source-verified NPC actions instead of discarding them.
        missing_date = _normalized_chronology(
            {"chronology": [{
                "date": "25.09.2026",
                "summary": "Другая дата без сведений о расследованиях.",
                "importance": "normal",
            }]},
            source_chronology=full,
        )
        recovery = next(x for x in missing_date if x.get("story_date") == "24.09.2026")
        assert {a["actor_character_id"] for a in recovery["actor_events"]} == {"daren", "var"}
        assert not recovery.get("participants_present")



def test_fifteen_turn_scene_compaction_does_not_hide_independent_actor_event():
    from app import session_runtime, writer_first_runtime

    source_event = {
        "event_id": "actor-before-15",
        "turn_number": 4,
        "story_date": "24.09.2026",
        "event": "Дарен нашёл след, о котором POV ничего не знает.",
        "actor_character_id": "daren",
        "importance": "major",
        "compacted_scene_id": "scene_t1_t15",
        "participants_present": [],
    }
    later = [{
        "event_id": f"other-{turn}",
        "turn_number": turn,
        "story_date": "25.09.2026",
        "event": f"Постороннее событие {turn}.",
        "importance": "normal",
        "participants_present": ["pov"],
    } for turn in range(16, 101)]
    events = [source_event, *later]
    selected = session_runtime._select_chronology_context(
        events, relevant_character_ids=["daren"], location=None,
    )
    assert source_event in selected
    compact = writer_first_runtime._compact_chronology(
        selected, ["daren"], "другое место",
    )
    assert source_event in compact
    # Indexing an action must not imply that POV witnessed or learned about it.
    assert not source_event["participants_present"]



def test_large_offscreen_actor_catalog_is_not_repeated_in_every_pov_packet():
    from copy import deepcopy
    from app import writer_first_runtime, session_runtime

    actors = [f"offscreen_{number}" for number in range(70)]
    rows = [{
        "event_id": f"macro_{i}",
        "turn_number": i * 60,
        "story_date": f"0{i}.10.2026",
        "event": f"Сводка по самостоятельным действиям NPC в день {i}.",
        "importance": "major",
        "participants_present": [],
        "actor_events": [{
            "actor_character_id": actor,
            "source_event_id": f"e_{i}_{number}",
            "turn_number": i * 60 - 1,
            "event": ("Самостоятельное расследование с подтверждёнными последствиями. " * 4),
        } for number, actor in enumerate(actors)],
    } for i in range(1, 7)]
    raw_size = len(json.dumps(rows, ensure_ascii=False))
    saved_copy = deepcopy(rows)
    # Only the POV participates. Keep compact dated summaries without sending
    # the full life history of seventy absent actors with every new scene.
    current = writer_first_runtime._compact_chronology(rows, ["pov"], "дом POV")
    current_size = len(json.dumps(current, ensure_ascii=False))
    assert current_size < raw_size // 8
    assert all("actor_events" not in row for row in current)

    # Once an NPC becomes a scene actor, their provenance is retrievable,
    # but unrelated characters' full histories are still not in the packet.
    selected = session_runtime._select_chronology_context(
        rows, relevant_character_ids=["offscreen_17"], location=None,
    )
    result = writer_first_runtime._compact_chronology(
        selected, ["pov", "offscreen_17"], "другое место",
    )
    assert result
    assert all(
        action["actor_character_id"] == "offscreen_17"
        for row in result
        for action in row.get("actor_events", [])
    )
    assert sum(len(row.get("actor_events", [])) for row in result) == len(rows)
    # Never mutate the canonical actor history.
    assert rows == saved_copy



def test_day_one_significant_facts_survive_day_sixty_even_when_summary_omits_them():
    from app import session_runtime, writer_first_runtime

    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        root = storage.SESSIONS_DIR / "day-one"
        root.mkdir(parents=True)
        storage._write_json(root / "source.json", {
            "version": 5, "profile_schema": {"version": 1},
        })
        turns = [_turn(i, "24.09.2026" if i <= 10 else "25.09.2026")
                 for i in range(1, 61)]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns),
            encoding="utf-8",
        )
        first_day = [
            {"event_id": "first-meeting", "turn_number": 1,
             "story_date": "24.09.2026", "importance": "anchor",
             "participants_present": ["pov", "npc"],
             "event": "POV впервые познакомился с Мирой у северного входа."},
            {"event_id": "first-promise", "turn_number": 2,
             "story_date": "24.09.2026", "importance": "major",
             "participants_present": ["pov", "npc"],
             "event": "POV обещал Мире не раскрывать её тайну."},
            {"event_id": "first-secret", "turn_number": 3,
             "story_date": "24.09.2026", "importance": "major",
             "participants_present": ["pov"],
             "event": "POV узнал, что зеркало опасно для детей."},
            {"event_id": "ordinary-tea", "turn_number": 4,
             "story_date": "24.09.2026", "importance": "normal",
             "event": "POV выпил чай."},
        ]
        macro = apply_macro_chronology_compaction(
            root, first_day,
            {"chronology_compactions": [{
                "date": "24.09.2026",
                "summary": "POV познакомился с Мирой; знакомство состоялось в первый день.",
                "importance": "anchor", "participants": ["pov", "npc"],
            }]},
            end_turn=60,
        )
        assert len(macro) == 1
        row = macro[0]
        assert {v["source_event_id"] for v in row["source_key_facts"]} == {
            "first-meeting", "first-promise", "first-secret",
        }
        assert "ordinary-tea" not in {
            v["source_event_id"] for v in row["source_key_facts"]
        }
        # At 100, the dated macro and its preserved independent facts are
        # still available even if the summary left out the promise and secret.
        later = [
            {"event_id": f"later-{i}", "turn_number": i,
             "event": f"Новое событие {i}", "importance": "normal"}
            for i in range(61, 101)
        ]
        selected = session_runtime._select_chronology_context(
            [*macro, *later], relevant_character_ids=["pov"], location=None,
        )
        chosen = next(row for row in selected if row["event_id"] == macro[0]["event_id"])
        assert any("обещал" in v["event"] for v in chosen["source_key_facts"])
        assert any("зеркало" in v["event"] for v in chosen["source_key_facts"])
        writer = writer_first_runtime._compact_chronology(
            [*macro, *later], ["pov"], "дом",
        )
        chosen_writer = next(row for row in writer if row["event_id"] == macro[0]["event_id"])
        assert any("обещал" in v["event"] for v in chosen_writer["source_key_facts"])
