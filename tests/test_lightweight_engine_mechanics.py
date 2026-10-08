"""Lightweight mechanics: no new commit gates, independent NPCs and lossless canon."""
from copy import deepcopy

from app import continuation_runtime, long_horizon_audit, scene_engine_hints, session_runtime, writer_first_runtime


def test_cues_rotate_offscreen_cast_without_requiring_an_event():
    rows = [
        {"character_id": f"npc_{index}", "offscreen_can_initiate": True, "importance": "recurring"}
        for index in range(6)
    ] + [{"character_id": "present_npc", "present": True}]
    state = {"current": {"unfinished_actions": ["Завтра встреча", "Ожидается звонок"]},
             "threads": [{"status": "active", "summary": "Рабочий конфликт"},
                         {"status": "resolved", "summary": "Закрытая линия"}]}
    first = scene_engine_hints.build(state, rows, 0)
    second = scene_engine_hints.build(state, rows, 1)
    assert first["advisory_only"] is True
    assert first["offscreen_focus_ids"] != second["offscreen_focus_ids"]
    assert len(first["offscreen_focus_ids"]) == 3
    assert first["physical_relationship_review_ids"] == ["present_npc"]
    assert first["active_threads"] == ["Рабочий конфликт"]
    assert not any("required" in key for key in first)


def test_offscreen_actor_is_not_invented_as_pov_witness_or_pov_location():
    state = {
        "current": {"date": "04.04.2026", "time": "22:00", "location": "Кухня",
                    "present_characters": ["pov"]},
        "characters": {"actor": {"location": "Вокзал"}},
    }
    cards = [{"character_id": "pov", "name": "POV"},
             {"character_id": "actor", "name": "Дантэ"}]
    normalized = session_runtime._normalise_chronology_events(
        [{"event": "Дантэ самостоятельно уехал.", "actor_character_id": "actor",
          "importance": "normal"}],
        turn_number=12, state=state, cards=cards,
    )
    assert len(normalized) == 1
    row = normalized[0]
    assert row["actor_character_id"] == "actor"
    assert row.get("participants_present", []) == []
    assert row["location"] == "Вокзал"


def test_actor_history_recalls_early_fact_without_leaking_other_actors():
    rows = [
        {"event_id": "first", "turn_number": 1, "event": "Старое обещание",
         "actor_character_id": "npc", "importance": "normal"},
    ] + [
        {"event_id": f"later_{i}", "turn_number": i + 2,
         "event": "Позднее событие", "actor_character_id": "npc"}
        for i in range(24)
    ]
    rows += [{"event_id": "macro", "turn_number": 60, "importance": "anchor",
              "event": "Итоги дня",
              "source_key_facts": [
                  {"event_id": "own", "event": "Дантэ ушёл", "actor_character_id": "npc"},
                  {"event_id": "foreign", "event": "Эдриан позвонил", "actor_character_id": "other"},
              ]}]
    selected = writer_first_runtime._compact_chronology(rows, ["npc"], "Нет")
    assert any(x.get("event_id") == "first" for x in selected)
    macro = next(x for x in selected if x.get("event_id") == "macro")
    assert [r["event_id"] for r in macro["source_key_facts"]] == ["own"]


def test_macro_keeps_true_actor_evidence_without_new_compaction_gate():
    source = {"version": 5}
    turns = [
        {"turn_number": 1, "extracted": {"state_patch": {"current": {"date": "01.04.2026"}}}},
        {"turn_number": 2, "extracted": {"state_patch": {"current": {"date": "02.04.2026"}}}},
    ]
    chronology = [
        {"event_id": "important", "turn_number": 1, "story_date": "01.04.2026",
         "event": "Эдриан признал ошибку", "importance": "major"},
        {"event_id": "actor", "turn_number": 2, "story_date": "02.04.2026",
         "actor_character_id": "dante", "event": "Дантэ самостоятельно отправил сообщение",
         "participants_present": [], "importance": "normal"},
    ]
    repairs = {"chronology_compactions": [
        {"date": "01.04.2026", "summary": "Эдриан признал свою ошибку перед Ринатой."}
    ]}
    result = long_horizon_audit._apply_macro_chronology_compaction_core(
        source, turns, chronology, repairs, end_turn=60,
    )
    macro = next(x for x in result if x.get("canonical_macro_compaction"))
    assert macro["source_key_facts"][0]["event_id"] == "important"
    assert any(x.get("event_id") == "actor" for x in result)


def test_continuation_preserves_owner_only_journal_and_dated_source_facts():
    source_memory = {"characters": {
        "npc": {
            "knowledge_journal": [{"entry_id": "first", "text": "Секрет", "turn": 1}],
            "knowledge": [{"fact_id": "k1", "fact": "Обещание", "learned_turn": 1}],
        },
        "other": {
            "knowledge_journal": [{"entry_id": "other", "text": "Чужое знание", "turn": 1}],
        },
    }}
    normalized = {"characters": {
        "npc": {"knowledge": [], "knowledge_journal": []},
        "other": {"knowledge": [], "knowledge_journal": []},
    }}
    saved = continuation_runtime._preserve_personal_facts(normalized, source_memory)
    assert saved["characters"]["npc"]["knowledge_journal"][0]["text"] == "Секрет"
    assert saved["characters"]["npc"]["knowledge"][0]["fact"] == "Обещание"
    assert all(x["entry_id"] != "other" for x in saved["characters"]["npc"]["knowledge_journal"])
    assert normalized["characters"]["npc"]["knowledge_journal"] == []
    history = continuation_runtime._preserve_chronology_evidence(
        [], [{"event_id": "evt", "event": "Решающий разговор", "story_date": "01.04.2026",
              "importance": "anchor", "turn_number": 1}]
    )
    assert history[0]["source_key_facts"][0]["event_id"] == "evt"


def test_missing_signals_produce_no_forced_change():
    output = scene_engine_hints.build(
        {"current": {"unfinished_actions": []}, "threads": {}}, [], 110,
    )
    assert output["active_threads"] == []
    assert output["offscreen_focus_ids"] == []
    assert output["unfinished_actions"] == []
    assert output["advisory_only"] is True
