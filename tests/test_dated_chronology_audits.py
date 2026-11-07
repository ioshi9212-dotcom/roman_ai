"""Date recaps and private memory survive scheduled 15/60/120-turn maintenance."""
from copy import deepcopy
import json

import pytest

from app import fast_audit_runtime, operation_service, session_documents, storage
from app.long_horizon_audit import (MacroChronologyMissingDates, apply_macro_chronology_compaction,
                                   build_macro_payload, replay_macro_chronology_compaction)
from app.scene_compaction_runtime import apply_audit_compactions
from test_generator_state_documents import create, prepare, story
from test_scene_audit_compaction import one_scene


def test_native_recap_uses_event_date_and_only_causal_clock(story):
    sid, root = create(story)
    records = [
        {"event_id": "earlier", "turn_number": 1, "story_date": "01.09.2026",
         "event": "В городе началась забастовка.", "time": "10:00"},
        {"event_id": "deadline", "turn_number": 2, "story_date": "02.09.2026",
         "event": "Участники договорились о сроке возвращения.", "time_critical": True, "exact_time": "18:30"},
    ]
    storage._write_json(root / "chronology.json", records)
    native = session_documents.read(root / "хронология.json")
    assert set(native["дни"]) == {"01.09.2026", "02.09.2026"}
    assert "10:00" not in json.dumps(native["дни"], ensure_ascii=False)
    assert "18:30" in json.dumps(native["дни"], ensure_ascii=False)
    assert all(set(periods) == {"события"} for periods in native["дни"].values())
    assert storage._read_json(root / "chronology.json", []) == records


def test_journal_compaction_is_compact_in_view_and_lossless_on_disk(story):
    sid, root = create(story)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    originals = [
        {"entry_id": "j1", "date": "01.09.2026", "turn": 2, "text": "Со слов Елены: письмо может быть поддельным.", "confidence": "uncertain"},
        {"entry_id": "j2", "date": "01.09.2026", "turn": 3, "text": "Со слов Елены: письмо может быть поддельным.", "confidence": "uncertain"},
        {"entry_id": "j3", "date": "01.09.2026", "turn": 4, "text": "Рен сам видел сломанную печать на конверте."},
    ]
    storage._memory_bucket(memory, "ren")["knowledge_journal"] = deepcopy(originals)
    repairs = {"scene_compactions": [one_scene(1, 15)],
               "memory_compactions": [{"character_id": "ren", "memory_type": "knowledge_journal",
                "source_ids": ["j1", "j2", "j3"], "summary": "Рен узнал о письме."}]}
    after, _, _, _ = apply_audit_compactions(root, repairs, start_turn=1, end_turn=15, memory=memory, chronology=[])
    storage._write_json(root / "memory.json", after)
    native = session_documents.read(root / "память" / "ren.json")
    visible = native["знания"]["01.09.2026"]
    assert len(visible) == 1
    assert visible[0].count("письмо может быть поддельным") == 1
    assert "Со слов Елены" in visible[0] and "сам видел сломанную печать" in visible[0]
    restored = storage._read_json(root / "memory.json", {})["characters"]["ren"]["knowledge_journal"]
    for original in originals:
        saved = next(row for row in restored if row["entry_id"] == original["entry_id"])
        assert all(saved[key] == value for key, value in original.items())
    assert next(row for row in restored if row.get("canonical_compaction"))["confidence"] != "certain"
    _, packet = prepare(sid)
    assert packet["character_memory"]["ren"]["знания"]["01.09.2026"] == visible
    assert len(storage._read_json(root / "memory.json", {})["characters"]["ren"]["knowledge_journal"]) == 4


def test_120th_compaction_reviews_old_dates_and_flattens_source_evidence(story):
    sid, root = create(story)
    events = [{"event_id": "old", "turn_number": 2, "story_date": "01.09.2026",
               "event": "Закрылся единственный безопасный проход.", "importance": "major"}]
    first = apply_macro_chronology_compaction(root, events, {"chronology_compactions": [{
        "date": "01.09.2026", "summary": "После обвала единственный безопасный проход оказался закрыт."
    }]}, end_turn=60)
    events = first + [{"event_id": "new", "turn_number": 90, "story_date": "02.09.2026",
                       "event": "Открылась новая дорога через перевал.", "importance": "major"}]
    source = storage._read_json(root / "source.json", {})
    turns = [{"turn_number": n, "extracted": {"relationship_updates": [{"character_id": "ren"}]}} for n in (30, 90)]
    payload = build_macro_payload(root, source=source, state={}, chronology=events, turns=turns, end_turn=120)
    assert [row["turn_number"] for row in payload["relationship_changes_60_turns"]] == [90]
    assert payload["macro_range"] == [1, 120]
    assert payload["required_important_dates"] == ["01.09.2026", "02.09.2026"]
    with pytest.raises(MacroChronologyMissingDates):
        apply_macro_chronology_compaction(root, events, {"chronology_compactions": [{
            "date": "02.09.2026", "summary": "Открылась новая дорога через перевал, участники выбрали этот маршрут."
        }]}, end_turn=120)
    repairs = {"chronology_compaction_version": 2, "chronology_compactions": [
        {"date": "01.09.2026", "summary": "Старый проход был закрыт из-за обвала, что изменило дальнейший маршрут."},
        {"date": "02.09.2026", "summary": "Открылась новая дорога через перевал, участники выбрали этот маршрут."},
    ]}
    second = apply_macro_chronology_compaction(root, events, repairs, end_turn=120)
    assert len(second) == 2
    assert second[0]["source_key_facts"][0]["source_event_id"] == "old"
    assert "source_key_facts" not in second[0]["source_key_facts"][0]
    assert replay_macro_chronology_compaction(source, [], events, repairs, end_turn=120) == second
    # Pre-change saved audits retain their original 61..120 scope on replay.
    old_repairs = {"chronology_compactions": [repairs["chronology_compactions"][1]]}
    replayed = replay_macro_chronology_compaction(source, [], events, old_repairs, end_turn=120)
    assert replayed[0] == first[0]


def test_scheduled_audits_through_120_keep_general_history_separate_from_personal_facts(story):
    sid, root = create(story)
    seen_audits = []
    for turn in range(1, 121):
        date = "14.10.2026" if turn <= 60 else "15.10.2026"
        raw = f"Продолжить разговор {turn}"
        manifest, _ = prepare(sid, raw, request_id=f"dated-cycle-{turn}")
        changes = {"state_patch": {"current": {"date": date}}}
        if turn in {2, 62}:
            changes["chronology"] = [{"event_id": f"event-{turn}", "date": date,
                "event": "В кафе состоялся напряжённый разговор; спор остался нерешённым.",
                "importance": "major", **({"time_critical": True, "exact_time": "18:30"} if turn == 62 else {})}]
            changes["knowledge_journal_add"] = [{"character_id": "ren", "entry_id": f"learned-{turn}",
                "date": date, "text": f"Елена лично сказала Рену: ЛИЧНАЯ_ФРАЗА_{turn}.",
                "source_character_id": "pov", "confidence": "uncertain"}]
        result = operation_service.commit_turn_request(sid, {
            "packet_id": manifest["packet_id"], "user_input": raw,
            "scene_output": f"**Елена** — ЛИЧНАЯ_ФРАЗА_{turn}.",
            "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True,
                          "knowledge_reviewed": True, **changes}})
        if turn % 15:
            continue
        assert result["audit_available"] is True
        audit = fast_audit_runtime.get_audit_snapshot(sid)
        parts = [audit["content"]]
        for index in range(1, audit["chunk_count"]):
            parts.append(storage.get_turn_packet_chunk(sid, audit["audit_id"], index)["content"])
        review = json.loads("".join(parts))
        assert review["audit_range"] == [turn - 14, turn]
        repairs = {"scene_compactions": [one_scene(turn - 14, turn)]}
        if turn % 60 == 0:
            macro = review["macro_audit_60"]
            assert macro["macro_range"] == [1, turn]
            assert "learned-2" in json.dumps(macro["personal_memory_by_character"], ensure_ascii=False)
            original = next(row for row in macro["personal_memory_by_character"]["ren"]["knowledge_journal"] if row["entry_id"] == "learned-2")
            assert original["confidence"] == "uncertain" and original["source_character_id"] == "pov"
            dates = ["14.10.2026"] + (["15.10.2026"] if turn == 120 else [])
            repairs["chronology_compactions"] = [{"date": day,
                "summary": "В кафе состоялся напряжённый разговор; участники разошлись, не разрешив возникший спор."} for day in dates]
            bucket = storage._read_json(root / "memory.json", {})["characters"]["ren"]
            repairs["memory_compactions"] = [{"character_id": "ren", "memory_type": "knowledge_journal",
                "source_ids": [row["entry_id"]], "summary": row["text"]}
                for row in bucket["knowledge_journal"] if not row.get("superseded_by")]
        saved = operation_service.commit_audit_request(sid, {
            "audit_id": audit["audit_id"], "start_turn": turn - 14, "end_turn": turn,
            "repairs": repairs, "notes": []})
        assert saved["audited_through"] == turn
        seen_audits.append(turn)
    assert seen_audits == list(range(15, 121, 15))
    assert len(storage._read_turns(root)) == 120
    history = session_documents.read(root / "хронология.json")["дни"]
    assert set(history) == {"14.10.2026", "15.10.2026"}
    assert "ЛИЧНАЯ_ФРАЗА" not in json.dumps(history, ensure_ascii=False)
    assert "18:30" in json.dumps(history, ensure_ascii=False)
    memory = session_documents.read(root / "память" / "ren.json")["знания"]
    assert "ЛИЧНАЯ_ФРАЗА_2" in json.dumps(memory, ensure_ascii=False)
    assert "ЛИЧНАЯ_ФРАЗА_62" in json.dumps(memory, ensure_ascii=False)



def test_full_memory_review_can_compact_initial_knowledge_without_redating_it(story):
    sid, root = create(story)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    storage._memory_bucket(memory, "ren")["knowledge_journal"] = [
        {"entry_id": "initial", "turn": 0, "date": "01.09.2026", "text": "Со слов Елены: письмо может быть подделкой."}]
    repairs = {"scene_compactions": [one_scene(46, 60)], "memory_compactions": [{
        "character_id": "ren", "memory_type": "knowledge_journal",
        "source_ids": ["initial"], "summary": "Со слов Елены: письмо может быть подделкой."}]}
    after, _, _, _ = apply_audit_compactions(root, repairs, start_turn=46, end_turn=60, memory=memory, chronology=[])
    active = next(row for row in after["characters"]["ren"]["knowledge_journal"] if not row.get("superseded_by"))
    assert active["turn"] == 0 and active["date"] == "01.09.2026"


def test_journal_compaction_does_not_merge_different_learned_dates(story):
    sid, root = create(story)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    storage._memory_bucket(memory, "ren")["knowledge_journal"] = [
        {"entry_id": "first", "turn": 2, "date": "01.09.2026", "text": "Рен услышал первую версию."},
        {"entry_id": "second", "turn": 3, "date": "02.09.2026", "text": "Рен узнал другую версию."}]
    repairs = {"scene_compactions": [one_scene(1, 15)], "memory_compactions": [{
        "character_id": "ren", "memory_type": "knowledge_journal",
        "source_ids": ["first", "second"], "summary": "Рен узнал две версии."}]}
    before = deepcopy(memory)
    with pytest.raises(RuntimeError, match="MEMORY_COMPACTION_INVALID"):
        apply_audit_compactions(root, repairs, start_turn=1, end_turn=15, memory=memory, chronology=[])
    assert memory == before
