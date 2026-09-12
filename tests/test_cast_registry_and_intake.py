import json
import tempfile
from pathlib import Path

from app import cast_registry_runtime, draft_intake_runtime, novel_drafts, session_runtime, storage
from app.main import novel_draft_section_save
from app.models import NovelDraftSection
from fastapi import HTTPException
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


def test_chunked_large_intake_reconstructs_exact_raw_without_placeholder_or_summary():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("chunked_raw", "Chunked Raw", version=2)["draft_id"]
        raw = ("Первая часть с пробелами и переносом.\n" * 400) + "ФИНАЛ БЕЗ ПОТЕРЬ."
        chunks = [raw[index:index + 7000] for index in range(0, len(raw), 7000)]
        for index, chunk in enumerate(chunks):
            result = draft_intake_runtime.append_intake_chunk(
                draft_id,
                block_id="full_setup_001",
                stage="large_questionnaire",
                chunk_index=index,
                raw_text=chunk,
                is_last=index == len(chunks) - 1,
            )
        assert result["complete"] is True
        assert result["char_count"] == len(raw)
        draft = novel_drafts._read(draft_id)
        block = draft["sections"]["intake"]["blocks"][0]
        assert block["raw_text"] == raw
        assert block["fact_ids"] == []
        assert block["reviewed_against_raw"] is False
        assert draft["intake_uploads"]["full_setup_001"]["completed"] is True
        assert "chunks" not in draft["intake_uploads"]["full_setup_001"]


def test_chunk_upload_exact_retry_is_idempotent_and_conflicting_retry_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("chunk_retry", "Chunk Retry", version=2)["draft_id"]
        first = draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup", chunk_index=0, raw_text="ABC", is_last=False
        )
        assert first["next_chunk_index"] == 1
        retry = draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup", chunk_index=0, raw_text="ABC", is_last=False
        )
        assert retry["already_received"] is True
        with pytest.raises(ValueError, match="INTAKE_UPLOAD_CHUNK_CONFLICT"):
            draft_intake_runtime.append_intake_chunk(
                draft_id, block_id="b1", stage="setup", chunk_index=0, raw_text="XYZ", is_last=False
            )
        done = draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup", chunk_index=1, raw_text="DEF", is_last=True
        )
        assert done["complete"] is True
        replay = draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup", chunk_index=1, raw_text="DEF", is_last=True
        )
        assert replay["already_completed"] is True
        assert novel_drafts._read(draft_id)["sections"]["intake"]["blocks"][0]["raw_text"] == "ABCDEF"


def test_chunk_transport_rejects_placeholder_instead_of_storing_fake_full_text():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("placeholder", "Placeholder", version=2)["draft_id"]
        with pytest.raises(ValueError, match="INTAKE_PLACEHOLDER_FORBIDDEN"):
            draft_intake_runtime.append_intake_chunk(
                draft_id,
                block_id="b1",
                stage="setup",
                chunk_index=0,
                raw_text="САМА НОВЕЛЛА [полный текст анкеты пользователя сохранён дословно]",
                is_last=True,
            )
        draft = novel_drafts._read(draft_id)
        assert "intake" not in draft["sections"]


def test_pending_chunk_upload_blocks_read_and_finalize_until_last_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("pending", "Pending", version=2)["draft_id"]
        draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup", chunk_index=0, raw_text="Первая половина.", is_last=False
        )
        status = novel_drafts.draft_status(draft_id)
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] == "INTAKE_UPLOAD_INCOMPLETE"
        assert status["intake_uploads"]["pending_count"] == 1
        with pytest.raises(RuntimeError, match="INTAKE_UPLOAD_INCOMPLETE"):
            prepare_draft_read(draft_id)


def test_mapping_adds_only_existing_fact_ids_without_resending_raw_text():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("mapping", "Mapping", version=2)["draft_id"]
        raw = "Елена боится воды и не умеет плавать."
        draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="pov", chunk_index=0, raw_text=raw, is_last=True
        )
        save_section(draft_id, "foundation", json.dumps({
            "facts": [
                {"fact_id": "f_water", "text": "Елена панически боится воды", "source": "user_setup", "stored_in": ["characters"], "story_use": "continuity"},
                {"fact_id": "f_swim", "text": "Елена не умеет плавать", "source": "user_setup", "stored_in": ["characters"], "story_use": "continuity"},
            ],
            "hooks": [],
            "story_pillars": [],
        }, ensure_ascii=False))
        with pytest.raises(ValueError, match="INTAKE_FACT_ID_UNKNOWN"):
            draft_intake_runtime.update_intake_mapping(
                draft_id, "b1", fact_ids=["made_up"], reviewed_against_raw=True, contains_no_facts=False
            )
        draft_intake_runtime.update_intake_mapping(
            draft_id,
            "b1",
            fact_ids=["f_water", "f_swim"],
            reviewed_against_raw=True,
            contains_no_facts=False,
        )
        block = novel_drafts._read(draft_id)["sections"]["intake"]["blocks"][0]
        assert block["raw_text"] == raw
        assert block["fact_ids"] == ["f_water", "f_swim"]
        assert block["reviewed_against_raw"] is True


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


def test_intake_source_conflict_returns_actionable_http_error():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_storage(tmp)
        draft_id = create_draft("conflict_help", "Conflict Help", version=2)["draft_id"]
        save_section(draft_id, "intake", json.dumps({"blocks": [{
            "block_id": "b1",
            "stage": "pov",
            "raw_text": "Исходный текст.",
            "fact_ids": [],
            "reviewed_against_raw": False,
            "contains_no_facts": True,
        }]}, ensure_ascii=False))

        body = NovelDraftSection(
            section_name="intake",
            section_json=json.dumps({"blocks": [{
                "block_id": "b1",
                "stage": "pov",
                "raw_text": "Изменённый текст.",
                "fact_ids": [],
                "reviewed_against_raw": False,
                "contains_no_facts": True,
            }]}, ensure_ascii=False),
        )
        try:
            novel_draft_section_save(draft_id, body)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "new unique block_id" in str(exc.detail)
            assert "send only that new block" in str(exc.detail)
            assert "never reconstruct them from memory" in str(exc.detail)
        else:
            raise AssertionError("immutable intake conflict must return an actionable 409")


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
