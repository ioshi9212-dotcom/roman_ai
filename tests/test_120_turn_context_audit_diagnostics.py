"""Contract probes for Roman AI context transport and 15/60-turn audits.

These are synthetic sessions, NOT a claim about a deployed Amvera session.
Known current gaps are marked xfail so CI reports them without hiding the
working 120-turn archival and packet checks.
"""
import json
import tempfile
from pathlib import Path

import pytest

from app import audit_runtime, session_runtime, storage
from app.long_horizon_audit import build_macro_payload
from app.scene_compaction_runtime import (
    active_memory_records,
    apply_audit_compactions,
    complete_knowledge_records,
)


def _temp_storage(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel(*, detailed=False):
    markers = {
        "tattoos": "POV_TATTOO_FULL_BODY__" + ("t" * 22000),
        "appearance": "SILAS_APPEARANCE_MARKER__" + ("s" * 9000),
        "lore": "NOVEL_CANON_END_MARKER__" + ("l" * 17000),
    }
    characters = [
        {"character_id": "rinata", "name": "Рината", "is_pov": True,
         "appearance": {"tattoos": markers["tattoos"] if detailed else "татуировки по всему телу"}},
        {"character_id": "silas", "name": "Сайлас",
         "appearance": markers["appearance"] if detailed else "тёмные волосы"},
        {"character_id": "absent", "name": "Отсутствующий NPC",
         "bio": "OFFSCREEN_PRIVATE_CARD_MARKER"},
    ]
    return {
        "novel_id": "context-audit-120",
        "title": "Контрольный роман",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "rinata", "lore": markers["lore"] if detailed else "контрольный лор"},
        "source_intake": [{"block_id": "raw",
            "raw_text": (
                "Реакция Ринаты на Сайласа: при взглядах начинаются вспышки.\n"
                "Реакция Сайласа на Ринату: при сближении он ощущает тоску."
            )}],
        "characters": characters,
        "starting_state": {
            "pov": {"character_id": "rinata"},
            "current": {
                "date": "01.09.2026", "time": "10:00", "location": "room",
                "present_characters": ["rinata", "silas"],
            },
        },
    }, markers


def _packet(sid, text):
    manifest = session_runtime.prepare_turn_packet(sid, text)
    assert manifest["first_chunk_included"] is True
    assert manifest["chunk_chars_max"] == 16000
    pieces = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        response = storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
        assert response["chunk_index"] == index
        pieces.append(response["content"])
    assert all(len(part) == 16000 for part in pieces[:-1])
    assert len(pieces[-1]) <= 16000
    assert sum(map(len, pieces)) == manifest["total_chars"]
    assert storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})[
        "read_chunks"
    ] == list(range(manifest["chunk_count"]))
    return manifest, json.loads("".join(pieces)), "".join(pieces)


def _scene_summary(first, last):
    return [{
        "start_turn": first,
        "end_turn": last,
        "summary": (
            f"Сцена с {first}-го по {last}-й ход: Рината и Сайлас последовательно "
            "обсуждали события дня и закрепили последствия нескольких встреч; "
            "сведения остались доступными в неизменяемом архиве ходов."
        ),
        "participants": ["rinata", "silas"],
        "location": "room",
        "status": "closed",
    }]


def test_packet_has_all_chunks_full_active_cards_and_exact_author_reactions():
    with tempfile.TemporaryDirectory() as tmp:
        _temp_storage(tmp)
        novel, markers = _novel(detailed=True)
        sid = storage.create_session(novel)["session_id"]
        manifest, context, raw = _packet(sid, "Посмотреть на Сайласа")
        assert manifest["chunk_count"] >= 4
        assert markers["tattoos"] in raw
        assert markers["appearance"] in raw
        source = storage._read_json(storage.SESSIONS_DIR / sid / "source.json", {})
        assert context["novel"] == source["novel"]
        assert context["novel"]["lore"] == markers["lore"]
        assert markers["lore"] in raw
        assert "OFFSCREEN_PRIVATE_CARD_MARKER" not in raw
        assert {row["character_id"] for row in context["character_cards"]} == {
            "rinata", "silas"
        }
        assert set(context["character_memory"]) == {"rinata", "silas"}
        assert context["runtime_rules"]
        assert context["scene_builder"]
        assert context["scene_state"]
        assert context["chronology"] is not None
        assert context["relationships"] is not None
        assert context["cast_registry"]["characters"]
        reactions = context["author_reaction_cues"]["entries"]
        assert len(reactions) == 2
        assert any("Реакция Ринаты на Сайласа:" in row["verbatim_author_rule"]
                   for row in reactions)
        assert any("Реакция Сайласа на Ринату:" in row["verbatim_author_rule"]
                   for row in reactions)


@pytest.mark.parametrize("safe_opt_in", [False, True], ids=["existing-session", "safe-audit-v2"])
def test_120_turn_eight_audits_two_macro_60_and_raw_archive_survives(safe_opt_in):
    with tempfile.TemporaryDirectory() as tmp:
        _temp_storage(tmp)
        novel, _ = _novel()
        sid = storage.create_session(
            novel, meta_patch={"safe_audit_v2": True} if safe_opt_in else None
        )["session_id"]
        root = storage.SESSIONS_DIR / sid
        dates = ["01.09.2026", "02.09.2026", "03.09.2026", "04.09.2026"]
        landmark_turns = {2, 17, 32, 47, 62, 77, 92, 107}
        audits = []
        for turn in range(1, 121):
            day = dates[(turn - 1) // 30]
            if turn in (31, 61, 91):
                state_change = {"current": {"date": day}}
            else:
                state_change = {}
            chronology = [{
                "event_id": f"e{turn}",
                "event": f"Важное контрольное событие {turn} на дате {day}.",
                "date": day,
                "importance": "normal" if safe_opt_in else "major",
                "participants_present": ["rinata", "silas"],
            }] if turn in landmark_turns else []
            _packet(sid, f"Действие {turn}")
            committed = session_runtime.commit_turn(
                sid, {
                    "user_input": f"Действие {turn}",
                    "scene_output": (
                        f"🎭 Контрольный роман\n🕒 День {(turn - 1) // 30 + 1}, "
                        f"{day}, 10:00\n\nСцена {turn}.\n\n"
                        f"Состояние: спокойно\nОтношения:\n\n"
                        f"Ход {turn} · цикл {((turn - 1) % 15) + 1}/15"
                    ),
                    "extracted": {
                        "persistence_reviewed": True,
                        "chronology": chronology,
                        "knowledge_add": [],
                        "experiences_add": [],
                        "dialogue_memory_add": [],
                        "state_patch": state_change,
                    },
                },
            )
            assert committed["turn_number"] == turn
            if turn % 15:
                continue
            if safe_opt_in:
                with pytest.raises(RuntimeError, match="AUDIT_REQUIRED"):
                    session_runtime.prepare_turn_packet(sid, f"next-{turn + 1}")
            manifest = audit_runtime.get_audit_snapshot(sid)
            assert manifest["audit_range"] == [turn - 14, turn]
            for index in range(1, manifest["chunk_count"]):
                audit_runtime.get_audit_snapshot_chunk(
                    sid, manifest["audit_id"], index
                )
            repairs = {"scene_compactions": _scene_summary(turn - 14, turn)}
            if turn in (60, 120) and safe_opt_in:
                full = json.loads("".join([
                    manifest["content"],
                    *[
                        audit_runtime.get_audit_snapshot_chunk(
                            sid, manifest["audit_id"], i
                        )["content"]
                        for i in range(1, manifest["chunk_count"])
                    ],
                ]))
                required_ids = full["macro_audit_60"]["required_character_ids"]
                assert full["macro_audit_60"]["macro_range"] == [1, turn]
                assert set(required_ids) == {"rinata", "silas", "absent"}
                assert "full_personal_memory_review" in full["macro_audit_60"]
                repairs["macro_knowledge_review"] = {
                    "reviewed_character_ids": required_ids
                }
                repairs["memory_compactions"] = []
            if safe_opt_in and turn % 60:
                repairs["chronology_compactions"] = [{
                    "date": day, "summary": f"Событие этого окна произошло в день {day}."
                }]
            if turn in (60, 120):
                start = 0 if turn == 60 or safe_opt_in else 2
                repairs["chronology_compactions"] = [
                    {
                        "date": date,
                        "summary": (
                            f"В этот день участники пережили несколько важных "
                            f"событий, сохранив их хронологический порядок "
                            f"и последствия для дальнейшего развития истории: {date}."
                        ),
                        "participants": ["rinata", "silas"],
                    }
                    for date in dates[start:turn // 30]
                ]
            audited = session_runtime.commit_audit(sid, {
                "start_turn": turn - 14,
                "end_turn": turn,
                "repairs": repairs,
                "notes": [],
            })
            audit_runtime.clear_audit_packet(sid)
            audits.append(audited["audited_through"])
            if turn in (60, 120):
                assert audited["macro_chronology_compacted"] is True
        assert audits == [15, 30, 45, 60, 75, 90, 105, 120]
        meta = storage._read_json(root / "meta.json", {})
        assert meta["last_audit_turn"] == 120
        assert len(storage._read_turns(root)) == 120
        chronology = storage._read_json(root / "chronology.json", [])
        macro = [r for r in chronology if r.get("canonical_macro_compaction")]
        assert {r.get("story_date") for r in macro} == set(dates)
        for row in macro:
            assert row.get("source_key_facts")
        if safe_opt_in:
            assert {
                fact["turn_number"] for row in macro for fact in row["source_key_facts"]
            } == landmark_turns
        assert len(storage._read_json(root / "audits.json", [])) == 8
        # The 121st scene must still be possible after two macro audits.
        next_packet, context, _ = _packet(sid, "Продолжить на 121-м ходе")
        assert next_packet["prepared_for_turn"] == 121
        assert context["chronology"]


@pytest.mark.xfail(strict=True, reason="Known issue: due audits are currently optional/nonblocking")
def test_due_15_turn_audit_is_enforced_not_only_offered():
    # Contract expectation: at the 15-turn boundary gameplay must request
    # reconciliation, not merely expose a flag the writer may ignore.
    from app.turn_pipeline import commit_turn
    source = __import__("inspect").getsource(commit_turn)
    assert 'saved["audit_blocking"] = False' not in source
    assert "saved.pop(\"required_audit\", None)" not in source


@pytest.mark.xfail(strict=True, reason="Known issue: writer reconstructs superseded raw knowledge")
def test_writer_uses_compact_knowledge_after_15_audit_not_raw_sources():
    with tempfile.TemporaryDirectory() as tmp:
        _temp_storage(tmp)
        novel, _ = _novel()
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge"].extend([
            {"fact_id": "clue_1", "character_id": "silas",
             "learned_turn": 2, "fact": "Сайлас знает первый важный факт."},
            {"fact_id": "clue_2", "character_id": "silas",
             "learned_turn": 7, "fact": "Сайлас знает второй важный факт."},
        ])
        compacted, _, _, _ = apply_audit_compactions(
            root,
            {"scene_compactions": _scene_summary(1, 15),
             "memory_compactions": [{
                 "character_id": "silas", "memory_type": "knowledge",
                 "source_ids": ["clue_1", "clue_2"],
                 "summary": "Сайлас знает первый и второй важные факты.",
             }]},
            start_turn=1, end_turn=15, memory=memory, chronology=[],
        )
        rows = compacted["characters"]["silas"]["knowledge"]
        assert len(active_memory_records(rows)) == 1
        # Raw source records stay archived but should not expand in every packet.
        writer_rows = complete_knowledge_records(rows)
        assert len(writer_rows) == 1
        assert writer_rows[0].get("canonical_compaction") is True


@pytest.mark.xfail(strict=True, reason="Known issue: macro 60 reviews dates, not all personal knowledge")
def test_macro_60_reviews_full_character_memory_not_only_chronology():
    with tempfile.TemporaryDirectory() as tmp:
        _temp_storage(tmp)
        macro = build_macro_payload(
            Path(tmp), source={"version": 5}, state={},
            chronology=[], turns=[], end_turn=60,
        )
        assert macro is not None
        assert "personal_memory_compactions" in macro["output_required"]
