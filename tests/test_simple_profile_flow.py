import json
import tempfile
from pathlib import Path

import pytest

from fastapi import HTTPException

from app import draft_intake_runtime, novel_drafts, session_runtime, setup_draft_v3_runtime, simple_profile_runtime, storage
from app.novel_access import get_novel_read_chunk
from app.character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read
from app.scene_knowledge_read import get_character_knowledge_chunk, prepare_character_knowledge_read


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _read_working_draft(draft_id: str) -> None:
    manifest = novel_drafts.prepare_draft_read(draft_id)
    for index in range(manifest["chunk_count"]):
        get_novel_read_chunk(manifest["read_id"], index)


def _build_simple_draft(*, raw_text=None, silas_notes=None) -> str:
    draft = novel_drafts.create_draft("simple_profiles", "Пока мир не сгорит", version=5)
    draft_id = draft["draft_id"]

    draft_intake_runtime.append_intake_chunk(
        draft_id,
        block_id="raw_setup",
        stage="setup",
        chunk_index=0,
        raw_text=raw_text if raw_text is not None else (
            "POV — Рината Дейл, 21 год. Сайлас Вейн, 400 лет, часовщик. "
            "Сайлас скрывает свой возраст от Ринаты. История — мистический триллер."
        ),
        is_last=True,
    )

    revision = novel_drafts.draft_status(draft_id)["revision"]
    novel_drafts.save_section(
        draft_id,
        "novel",
        json.dumps({
            "genres": ["мистика", "триллер"],
            "pov": "rinata",
            "premise": "Рината сталкивается со скрытым сверхъестественным слоем города.",
        }, ensure_ascii=False),
        expected_revision=revision,
    )

    revision = novel_drafts.draft_status(draft_id)["revision"]
    novel_drafts.save_section(
        draft_id,
        "characters",
        json.dumps([
            {
                "id": "rinata",
                "full_name": "Рината Дейл",
                "age": 21,
                "status": "человек",
                "role": "POV",
                "is_pov": True,
            },
            {
                "id": "silas",
                "full_name": "Сайлас Вейн",
                "age": 400,
                "status": "не человек",
                "role": "главный",
                "profession": "часовщик",
                **({"notes": silas_notes} if silas_notes is not None else {}),
            },
        ], ensure_ascii=False),
        expected_revision=revision,
    )

    revision = novel_drafts.draft_status(draft_id)["revision"]
    novel_drafts.save_section(
        draft_id,
        "hidden_lore",
        json.dumps({"entries": ["Истинная природа Сайласа неизвестна Ринате."]}, ensure_ascii=False),
        expected_revision=revision,
    )

    revision = novel_drafts.draft_status(draft_id)["revision"]
    draft_intake_runtime.update_intake_mapping(
        draft_id,
        "raw_setup",
        fact_ids=[],
        reviewed_against_raw=True,
        contains_no_facts=False,
        expected_revision=revision,
    )

    _read_working_draft(draft_id)
    revision = novel_drafts.draft_status(draft_id)["revision"]
    setup_draft_v3_runtime.confirm_reconciliation(
        draft_id,
        expected_revision=revision,
        confirmed_against_raw=True,
        unresolved_conflicts=[],
    )
    return draft_id



def test_identical_v5_section_resave_does_not_invalidate_completed_full_read():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()

        before = novel_drafts.draft_status(draft_id)
        assert before["intake_coverage"]["full_read_current"] is True
        assert before["reconciliation_current"] is True
        revision = before["revision"]

        raw = novel_drafts._read(draft_id)
        characters = raw["sections"]["characters"]
        result = novel_drafts.save_section(
            draft_id,
            "characters",
            json.dumps(characters, ensure_ascii=False),
            expected_revision=revision,
        )

        after = novel_drafts.draft_status(draft_id)
        assert result["section_changed"] is False
        assert result["idempotent_replay"] is True
        assert result["draft_revision"] == revision
        assert after["revision"] == revision
        assert after["intake_coverage"]["full_read_current"] is True
        assert after["reconciliation_current"] is True
        assert after["ready_to_finalize"] is True


def test_finalized_source_keeps_verbatim_trigger_without_bloating_gameplay():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        trigger = (
            "  При начале близости с Ринатой у Сайласа могут появляться короткие\n"
            "обрывки памяти первой жизни. Он пока не понимает их происхождения.  "
        )
        # This old instruction must remain in the audit source, even though the
        # working-canon migration removes it.
        raw = trigger + "\nЕсли игрок не дал реплику, не придумывай её.\n"
        draft_id = _build_simple_draft(raw_text=raw, silas_notes=trigger)
        novel_drafts.finalize_draft(draft_id)
        finalized = novel_drafts._read(draft_id)
        template = finalized["finalized_template"]
        assert template["source_intake"][-1] == {
            "block_id": "raw_setup", "stage": "setup", "raw_text": raw,
        }
        manifest = novel_drafts.prepare_draft_read(draft_id)
        assert manifest["intake_archived_in_draft_only"] is False
        read_back = json.loads("".join(
            get_novel_read_chunk(manifest["read_id"], index)["content"]
            for index in range(manifest["chunk_count"])
        ))
        assert read_back["source_intake"][-1]["raw_text"] == raw
        setup_draft_v3_runtime.set_launch_state(
            draft_id, expected_finalized_revision=finalized["finalized_revision"],
            starting_state_json=json.dumps({
                "current": {"date": "05.10.2026", "time": "10:00", "location": "комната",
                            "present_characters": ["rinata", "silas"]},
                "pov": {"character_id": "rinata"},
            }, ensure_ascii=False),
        )
        sid = novel_drafts.create_session_from_draft(draft_id)["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "(Коснуться его ладони.)")
        chunks = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            chunks.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
        context = json.loads("".join(chunks))
        assert "source_intake" not in "".join(chunks)
        assert next(c for c in context["character_cards"] if c["character_id"] == "silas")["notes"].split() == trigger.split()
        # Source cleanup during prepare must not rewrite the archived RAW.
        assert storage.load_session(sid)["source"]["source_intake"][-1]["raw_text"] == raw


def test_actual_v5_section_change_still_invalidates_full_read_and_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()

        before = novel_drafts.draft_status(draft_id)
        revision = before["revision"]
        raw = novel_drafts._read(draft_id)
        characters = raw["sections"]["characters"]
        characters[1]["notes"] = "Новая реально добавленная деталь."

        result = novel_drafts.save_section(
            draft_id,
            "characters",
            json.dumps(characters, ensure_ascii=False),
            expected_revision=revision,
        )

        after = novel_drafts.draft_status(draft_id)
        assert result["section_changed"] is True
        assert result["idempotent_replay"] is False
        assert after["revision"] == revision + 1
        assert after["intake_coverage"]["full_read_current"] is False
        assert after["reconciliation_current"] is False
        assert after["finalize_blocker"] == "INTAKE_FINAL_READ_REQUIRED"

def test_v5_setup_uses_fixed_profiles_without_foundation_or_initial_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()

        status = novel_drafts.draft_status(draft_id)
        assert status["simple_profile_mode"] is True
        assert status["ready_to_finalize"] is True
        assert status["foundation_coverage"]["required"] is False

        result = novel_drafts.finalize_draft(draft_id)
        assert result["content_finalized"] is True

        raw = novel_drafts._read(draft_id)
        template = raw["finalized_template"]
        assert template["knowledge"] == {}
        assert "foundation" not in template
        assert template["hidden_lore"]["entries"] == ["Истинная природа Сайласа неизвестна Ринате."]

        rinata, silas = template["characters"]
        assert rinata["name"] == "Рината"
        assert rinata["surname"] == "Дейл"
        assert silas["name"] == "Сайлас"
        assert silas["surname"] == "Вейн"
        assert silas["work"] == "часовщик"
        assert set(silas) == set(template["profile_schema"]["character_fields"])


def test_v5_session_packet_exposes_profiles_and_full_read_contract_without_fact_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()
        novel_drafts.finalize_draft(draft_id)

        finalized_revision = novel_drafts._read(draft_id)["finalized_revision"]
        setup_draft_v3_runtime.set_launch_state(
            draft_id,
            expected_finalized_revision=finalized_revision,
            starting_state_json=json.dumps({
                "current": {
                    "date": "24.09.2026",
                    "time": "09:10",
                    "location": "квартира",
                    "present_characters": ["rinata", "silas"],
                },
                "pov": {"character_id": "rinata"},
            }, ensure_ascii=False),
        )
        session = novel_drafts.create_session_from_draft(draft_id)
        sid = session["session_id"]

        memory = storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        assert memory["characters"]["rinata"]["knowledge_journal"] == []
        assert memory["characters"]["silas"]["knowledge_journal"] == []

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на Сайласа)")
        chunks = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            row = storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
            chunks.append(row["content"])
        context = json.loads("".join(chunks))

        assert context["character_profiles"]["silas"].startswith("Имя: Сайлас")
        assert "Возраст: 400" in context["character_profiles"]["silas"]
        assert "knowledge_journals" not in context
        assert context["speaker_context"]["silas"]["knowledge_source"]["kind"] == "mandatory_complete_knowledge_read"
        assert context["speaker_context"]["silas"]["knowledge_source"]["character_id"] == "silas"
        assert "knowledge_firewall_v5" not in context
        assert "dialogue_frames" not in context
        assert "source_fact_ids" not in context.get("speaker_context", {}).get("silas", {})
        causality = context["scene_logic_guardrails"]["knowledge_causality"]
        assert causality["source_before_use"] is True
        assert causality["no_retroactive_justification"] is True
        assert causality["character_knowledge_is_closed_world"] is True
        assert "speaker_context[character_id].profile_path" in causality["allowed_sources"][0]
        assert any("parenthetical" in item for item in causality["author_only_not_character_knowledge"])
        assert "собственный profile" in causality["rule"]
        assert "Нельзя оправдывать знание задним числом" in causality["rule"]
        review = context["scene_logic_guardrails"]["knowledge_review"]
        assert review["mandatory"] is True
        assert "источника не было до использования" in review["rule"]


def test_plain_journal_entries_are_persisted_without_model_supplied_ids():
    memory = {"characters": {}}
    updated = storage._apply_memory_events(
        memory,
        {
            "knowledge_journal_add": [
                {
                    "character_id": "silas",
                    "date": "24.09.2026",
                    "period": "утро",
                    "text": "Рината сказала Сайласу, что ей 19 лет.",
                }
            ]
        },
        turn_number=7,
    )
    entry = updated["characters"]["silas"]["knowledge_journal"][0]
    assert entry["text"] == "Рината сказала Сайласу, что ей 19 лет."
    assert entry["date"] == "24.09.2026"
    assert entry["period"] == "утро"
    assert entry["turn"] == 7
    assert entry["entry_id"].startswith("journal_t7_")


def test_v5_active_character_receives_complete_knowledge_journal_over_old_80_entry_limit():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()
        novel_drafts.finalize_draft(draft_id)

        finalized_revision = novel_drafts._read(draft_id)["finalized_revision"]
        setup_draft_v3_runtime.set_launch_state(
            draft_id,
            expected_finalized_revision=finalized_revision,
            starting_state_json=json.dumps({
                "current": {
                    "date": "24.09.2026",
                    "time": "09:10",
                    "location": "квартира",
                    "present_characters": ["rinata", "silas"],
                },
                "pov": {"character_id": "rinata"},
            }, ensure_ascii=False),
        )
        session = novel_drafts.create_session_from_draft(draft_id)
        sid = session["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge_journal"] = [
            {
                "entry_id": f"j{i}",
                "date": "24.09.2026",
                "period": "утро",
                "text": f"Знание номер {i}.",
                "turn": i,
            }
            for i in range(1, 121)
        ]
        storage._write_json(root / "memory.json", memory)

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на Сайласа)")
        chunks = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            row = storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
            chunks.append(row["content"])
        packet_text = "".join(chunks)
        context = json.loads(packet_text)

        assert "knowledge_journals" not in context
        assert "Знание номер 1." not in packet_text
        assert "Знание номер 120." not in packet_text

        knowledge = prepare_character_knowledge_read(sid, "silas")
        pieces = [knowledge["content"]]
        for index in range(1, knowledge["chunk_count"]):
            pieces.append(
                get_character_knowledge_chunk(sid, "silas", knowledge["read_id"], index)["content"]
            )
        payload = json.loads("".join(pieces))
        rows = payload["knowledge"]

        assert len(rows) == 120
        assert rows[0]["text"] == "Знание номер 1."
        assert rows[-1]["text"] == "Знание номер 120."


def test_v5_offscreen_bundle_receives_complete_knowledge_journal_too():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = _build_simple_draft()
        novel_drafts.finalize_draft(draft_id)

        finalized_revision = novel_drafts._read(draft_id)["finalized_revision"]
        setup_draft_v3_runtime.set_launch_state(
            draft_id,
            expected_finalized_revision=finalized_revision,
            starting_state_json=json.dumps({
                "current": {
                    "date": "24.09.2026",
                    "time": "09:10",
                    "location": "квартира",
                    "present_characters": ["rinata"],
                },
                "pov": {"character_id": "rinata"},
            }, ensure_ascii=False),
        )
        session = novel_drafts.create_session_from_draft(draft_id)
        sid = session["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "silas")
        bucket["knowledge_journal"] = [
            {
                "entry_id": f"j{i}",
                "date": "24.09.2026",
                "period": "утро",
                "text": f"Удалённое знание номер {i}.",
                "turn": i,
            }
            for i in range(1, 121)
        ]
        storage._write_json(root / "memory.json", memory)

        session_runtime.prepare_turn_packet(sid, "(ждать)")

        manifest = prepare_character_bundle_read(sid, "silas")
        pieces = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            pieces.append(get_character_bundle_chunk(sid, "silas", manifest["read_id"], index)["content"])
        bundle = json.loads("".join(pieces))

        assert "knowledge_journal" not in bundle
        assert bundle["knowledge_source"]["kind"] == "mandatory_complete_knowledge_read"

        knowledge = prepare_character_knowledge_read(sid, "silas")
        knowledge_pieces = [knowledge["content"]]
        for index in range(1, knowledge["chunk_count"]):
            knowledge_pieces.append(
                get_character_knowledge_chunk(sid, "silas", knowledge["read_id"], index)["content"]
            )
        payload = json.loads("".join(knowledge_pieces))
        rows = payload["knowledge"]

        assert len(rows) == 120
        assert rows[0]["text"] == "Удалённое знание номер 1."
        assert rows[-1]["text"] == "Удалённое знание номер 120."


def _private_message_v5_novel():
    return {
        "novel_id": "v5-private-message",
        "title": "Private Message",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "rinata"},
        "characters": [
            {"character_id": "rinata", "name": "Рината", "is_pov": True},
            {"character_id": "dante", "name": "Дантэ", "role": "друг"},
            {"character_id": "enzhe", "name": "Энже", "role": "знакомая"},
            {"character_id": "adrian", "name": "Эдриан", "role": "друг"},
            {"character_id": "silas", "name": "Сайлас", "role": "незнакомец"},
        ],
        "starting_state": {
            "pov": {"character_id": "rinata"},
            "current": {
                "location": "зал",
                "present_characters": ["rinata", "enzhe", "adrian"],
            },
        },
    }


def test_v5_private_message_content_is_hard_blocked_for_bystander_but_allowed_for_recipient():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_private_message_v5_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        user_input = (
            "(проигнорировать Энже и написать Дантэ - соскучилась. "
            "Привези мне шоколадное пирожное. У меня потребность в сладком. Жду. "
            "Встать и подойти к Энже) Че притащила? Сладкое есть?"
        )

        with pytest.raises(HTTPException) as exc:
            simple_profile_runtime._validate_simple_private_input_boundary(
                root,
                {
                    "user_input": user_input,
                    "scene_output": "**Энже** — Я тебе шоколадное пирожное принесла.",
                    "extracted": {"knowledge_journal_add": []},
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "V5_PRIVATE_INPUT_KNOWLEDGE_LEAK"
        assert exc.value.detail["character_id"] == "enzhe"

        simple_profile_runtime._validate_simple_private_input_boundary(
            root,
            {
                "user_input": user_input,
                "scene_output": "**Дантэ** — Шоколадное пирожное привезу.",
                "extracted": {"knowledge_journal_add": []},
            },
        )

        simple_profile_runtime._validate_simple_private_input_boundary(
            root,
            {
                "user_input": user_input,
                "scene_output": "**Энже** — Сладкое есть.",
                "extracted": {"knowledge_journal_add": []},
            },
        )


def test_v5_private_pov_name_does_not_become_bystander_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_private_message_v5_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        with pytest.raises(HTTPException) as exc:
            simple_profile_runtime._validate_simple_private_input_boundary(
                root,
                {
                    "user_input": "(Сайлас. Фокусник хуев. Хрен тебе, а не реакция.) Я тебе номер не дам.",
                    "scene_output": "**Эдриан** — Сайлас? Кто это?",
                    "extracted": {"knowledge_journal_add": []},
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "V5_PRIVATE_INPUT_KNOWLEDGE_LEAK"
        assert exc.value.detail["character_id"] == "adrian"


def test_v5_private_message_cannot_be_persisted_to_wrong_character_journal():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_private_message_v5_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        with pytest.raises(HTTPException) as exc:
            simple_profile_runtime._validate_simple_private_input_boundary(
                root,
                {
                    "user_input": "(написать Дантэ - привези шоколадное пирожное)",
                    "scene_output": "**Энже** — Ладно.",
                    "extracted": {
                        "knowledge_journal_add": [
                            {"character_id": "enzhe", "text": "Рината попросила Дантэ привезти шоколадное пирожное."}
                        ]
                    },
                },
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "V5_PRIVATE_INPUT_JOURNAL_LEAK"


def test_v5_explicit_recipient_may_persist_addressed_message_content():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_private_message_v5_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        simple_profile_runtime._validate_simple_private_input_boundary(
            root,
            {
                "user_input": "(написать Дантэ - привези шоколадное пирожное)",
                "scene_output": "**Дантэ** — Хорошо.",
                "extracted": {
                    "knowledge_journal_add": [
                        {"character_id": "dante", "text": "Рината попросила привезти шоколадное пирожное."}
                    ]
                },
            },
        )
