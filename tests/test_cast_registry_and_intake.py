import json
import tempfile
from pathlib import Path

from app import cast_registry_runtime, draft_intake_runtime, novel_drafts, session_runtime, storage
from app.novel_access import get_novel_read_chunk
from app.novel_drafts import create_draft, create_session_from_draft, finalize_draft, prepare_draft_read, save_section


def _setup_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _read_working_draft_fully(draft_id: str):
    manifest = prepare_draft_read(draft_id)
    for index in range(manifest["chunk_count"]):
        get_novel_read_chunk(manifest["read_id"], index)
    return manifest


def test_intake_merge_is_additive_and_raw_source_is_immutable():
    first = {"blocks": [{"block_id": "b1", "stage": "pov", "raw_text": "Она не любит молоко и боится темноты.", "fact_ids": ["f1"], "reviewed_against_raw": False}]}
    second = {"blocks": [
        {"block_id": "b1", "stage": "pov", "raw_text": "Она не любит молоко и боится темноты.", "fact_ids": ["f2"], "reviewed_against_raw": True},
        {"block_id": "b2", "stage": "characters", "raw_text": "Рен работает врачом.", "fact_ids": ["f3"], "reviewed_against_raw": True},
    ]}
    merged = draft_intake_runtime._merge_intake(first, second)
    assert [row["block_id"] for row in merged["blocks"]] == ["b1", "b2"]
    assert merged["blocks"][0]["fact_ids"] == ["f1", "f2"]
    assert merged["blocks"][0]["reviewed_against_raw"] is True

    changed_source = {"blocks": [{"block_id": "b1", "stage": "pov", "raw_text": "другой текст", "fact_ids": ["f1"], "reviewed_against_raw": True}]}
    try:
        draft_intake_runtime._merge_intake(merged, changed_source)
    except ValueError as exc:
        assert str(exc) == "INTAKE_BLOCK_SOURCE_IMMUTABLE"
    else:
        raise AssertionError("raw intake source must be immutable")


def test_intake_coverage_requires_raw_review_and_real_foundation_fact_ids():
    draft = {"sections": {
        "intake": {"blocks": [{"block_id": "b1", "stage": "history", "raw_text": "Большой кусок истории", "fact_ids": ["f1", "missing"], "reviewed_against_raw": False}]},
        "foundation": {"facts": [{"fact_id": "f1", "text": "Факт"}]},
    }}
    coverage = draft_intake_runtime._coverage(draft)
    assert coverage["ok"] is False
    assert coverage["unreviewed_blocks"] == ["b1"]
    assert coverage["unknown_fact_ids"] == [{"block_id": "b1", "fact_id": "missing"}]

    draft["sections"]["intake"]["blocks"][0]["reviewed_against_raw"] = True
    draft["sections"]["foundation"]["facts"].append({"fact_id": "missing", "text": "Ещё факт"})
    assert draft_intake_runtime._coverage(draft)["ok"] is True


def test_unfinalized_large_draft_can_be_reread_in_chunks_without_losing_raw_intake():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("big_setup", "Big Setup", version=2)["draft_id"]
        raw = "деталь " * 5000
        intake = {"blocks": [{"block_id": "history-1", "stage": "history", "raw_text": raw, "fact_ids": ["f1"], "reviewed_against_raw": False}]}
        save_section(draft_id, "intake", json.dumps(intake, ensure_ascii=False))

        manifest = prepare_draft_read(draft_id)
        assert manifest["working_draft"] is True
        assert manifest["chunk_count"] > 1
        text = ""
        for index in range(manifest["chunk_count"]):
            text += get_novel_read_chunk(manifest["read_id"], index)["content"]
        snapshot = json.loads(text)
        assert snapshot["finalized"] is False
        assert snapshot["sections"]["intake"]["blocks"][0]["raw_text"] == raw


def test_finalized_session_keeps_raw_intake_in_draft_archive_only():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("archive_only", "Archive Only", version=2)["draft_id"]
        save_section(draft_id, "novel", json.dumps({"pov_character": "pov"}, ensure_ascii=False))
        save_section(draft_id, "characters", json.dumps([{"character_id": "pov", "name": "Елена", "is_pov": True}], ensure_ascii=False))
        save_section(draft_id, "lore", json.dumps({}, ensure_ascii=False))
        save_section(draft_id, "starting_state", json.dumps({
            "pov": {"character_id": "pov"},
            "current": {"date": "01.09.2026", "time": "10:00", "location": "room", "present_characters": ["pov"]},
        }, ensure_ascii=False))
        foundation = {
            "facts": [{"fact_id": "f1", "text": "Елена любит кофе", "stored_in": ["pov"], "story_use": "reference"}],
            "hooks": [],
            "story_pillars": [{"pillar_id": "p1", "label": "Жизнь Елены", "source_fact_ids": ["f1"]}],
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))
        raw = "Елена всегда пьёт кофе утром. " * 1000
        save_section(draft_id, "intake", json.dumps({"blocks": [{
            "block_id": "pov-1", "stage": "pov", "raw_text": raw, "fact_ids": ["f1"], "reviewed_against_raw": True,
        }]}, ensure_ascii=False))

        _read_working_draft_fully(draft_id)
        status = novel_drafts.draft_status(draft_id)
        assert status["intake_coverage"]["full_read_current"] is True
        result = finalize_draft(draft_id)
        assert result["intake_archived_in_draft_only"] is True
        draft = novel_drafts._read(draft_id)
        assert draft["sections"]["intake"]["blocks"][0]["raw_text"] == raw
        assert "intake" not in draft["finalized_template"]

        meta = create_session_from_draft(draft_id)
        source = storage._read_json(storage.SESSIONS_DIR / meta["session_id"] / "source.json", {})
        assert "intake" not in source
        assert source["foundation"]["facts"][0]["fact_id"] == "f1"


def test_player_created_cast_returns_even_with_low_relationship_and_dead_is_excluded():
    cards = [
        {"character_id": "pov", "name": "Елена", "is_pov": True},
        {"character_id": "old_friend", "name": "Саша", "role": "friend"},
        {"character_id": "dead", "name": "Игорь", "role": "brother", "status": "dead"},
    ]
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"]},
        "characters": {},
        "relationships": {"old_friend": {"доверие": 0}},
        "world": {"cast_registry": {
            "old_friend": {"character_id": "old_friend", "name": "Саша", "origin": "player_created", "status": "active", "last_appearance_turn": 2},
            "dead": {"character_id": "dead", "name": "Игорь", "origin": "player_created", "status": "dead", "last_appearance_turn": 0},
        }},
    }
    pressure = cast_registry_runtime._rotation_pressure(state, cards, current_turn=20)
    ids = [row["character_id"] for row in pressure]
    assert "old_friend" in ids
    assert "dead" not in ids
    row = next(row for row in pressure if row["character_id"] == "old_friend")
    assert row["origin"] == "player_created"
    assert row["turns_since_appearance"] == 18


def test_strong_relationship_and_open_intent_raise_frequency_without_being_required_for_original_cast():
    cards = [
        {"character_id": "pov", "name": "Елена", "is_pov": True},
        {"character_id": "close", "name": "Лиам"},
        {"character_id": "intent", "name": "Сара"},
    ]
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"]},
        "characters": {},
        "relationships": {"close": {"доверие": 8}},
        "npc_intents": {"intent": {"ask": {"status": "active", "summary": "поговорить"}}},
        "world": {"cast_registry": {
            "close": {"character_id": "close", "origin": "player_created", "status": "active", "last_contact_turn": 5},
            "intent": {"character_id": "intent", "origin": "player_created", "status": "active", "last_contact_turn": 9},
        }},
    }
    pressure = cast_registry_runtime._rotation_pressure(state, cards, current_turn=10)
    ids = {row["character_id"] for row in pressure}
    assert ids == {"close", "intent"}
    assert next(row for row in pressure if row["character_id"] == "close")["relationship_salience"] == 0.8
    assert next(row for row in pressure if row["character_id"] == "intent")["open_intent"] is True


def test_new_story_npc_can_be_registered_without_replacing_original_cast():
    cards = [
        {"character_id": "pov", "name": "Елена", "is_pov": True},
        {"character_id": "original", "name": "Лиам", "role": "commander"},
        {"character_id": "new_doc", "name": "Марк", "role": "doctor"},
    ]
    state = {
        "pov": {"character_id": "pov"},
        "current": {"present_characters": ["pov"]},
        "characters": {},
        "relationships": {},
        "world": {"cast_registry": {"new_doc": {
            "character_id": "new_doc", "name": "Марк", "role": "doctor", "origin": "story_created", "status": "active", "first_registered_turn": 12, "last_appearance_turn": 12,
        }}},
    }
    registry = cast_registry_runtime._ensure_registry(state, cards, current_turn=20)
    assert registry["original"]["origin"] == "player_created"
    assert registry["new_doc"]["origin"] == "story_created"



def test_finalized_draft_can_reopen_for_pre_game_correction_and_new_session_keeps_draft_link():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("repairable", "Repairable", version=2)["draft_id"]
        save_section(draft_id, "novel", json.dumps({"pov_character": "pov"}, ensure_ascii=False))
        save_section(draft_id, "characters", json.dumps([
            {"character_id": "pov", "name": "Елена", "is_pov": True},
            {"character_id": "itan", "name": "Итан"},
        ], ensure_ascii=False))
        save_section(draft_id, "lore", json.dumps({}, ensure_ascii=False))
        save_section(draft_id, "starting_state", json.dumps({
            "pov": {"character_id": "pov"},
            "current": {"date": "01.09.2026", "time": "10:00", "location": "wrong", "present_characters": ["pov"]},
        }, ensure_ascii=False))
        save_section(draft_id, "foundation", json.dumps({
            "facts": [{"fact_id": "f1", "text": "Итан существует", "stored_in": ["characters"], "story_use": "continuity"}],
            "hooks": [],
            "story_pillars": [{"pillar_id": "p1", "label": "Итан", "source_fact_ids": ["f1"]}],
        }, ensure_ascii=False))
        save_section(draft_id, "intake", json.dumps({"blocks": [{
            "block_id": "b1", "stage": "cast", "raw_text": "Итан уже находится в секторе под прикрытием.",
            "fact_ids": ["f1"], "reviewed_against_raw": True,
        }]}, ensure_ascii=False))

        _read_working_draft_fully(draft_id)
        finalize_draft(draft_id)
        reopened = save_section(draft_id, "starting_state", json.dumps({
            "pov": {"character_id": "pov"},
            "current": {"date": "01.09.2026", "time": "10:00", "location": "Перед Восточным сектором", "present_characters": ["pov"]},
        }, ensure_ascii=False))
        assert reopened["reopened_from_finalized"] is True
        assert reopened["finalized"] is False
        assert "finalized_template" not in novel_drafts._read(draft_id)

        blocked = novel_drafts.draft_status(draft_id)
        assert blocked["ready_to_finalize"] is False
        assert blocked["finalize_blocker"] == "INTAKE_FINAL_READ_REQUIRED"
        _read_working_draft_fully(draft_id)
        finalize_draft(draft_id)
        meta = create_session_from_draft(draft_id)
        assert meta["source_draft_id"] == draft_id
        root = storage.SESSIONS_DIR / meta["session_id"]
        assert storage._read_json(root / "meta.json", {})["source_draft_id"] == draft_id
        resumed = session_runtime.continue_session(meta["session_id"])
        assert resumed["source_draft_id"] == draft_id
