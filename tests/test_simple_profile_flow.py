import json
import tempfile
from pathlib import Path

from app import draft_intake_runtime, novel_drafts, session_runtime, setup_draft_v3_runtime, storage
from app.novel_access import get_novel_read_chunk


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _read_working_draft(draft_id: str) -> None:
    manifest = novel_drafts.prepare_draft_read(draft_id)
    for index in range(manifest["chunk_count"]):
        get_novel_read_chunk(manifest["read_id"], index)


def _build_simple_draft() -> str:
    draft = novel_drafts.create_draft("simple_profiles", "Пока мир не сгорит", version=5)
    draft_id = draft["draft_id"]

    draft_intake_runtime.append_intake_chunk(
        draft_id,
        block_id="raw_setup",
        stage="setup",
        chunk_index=0,
        raw_text=(
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


def test_v5_session_packet_exposes_plain_profiles_and_journals_without_fact_ledger():
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
        index = manifest.get("next_chunk_index")
        while index is not None:
            row = storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
            chunks.append(row["content"])
            index = row.get("next_chunk_index")
        context = json.loads("".join(chunks))

        assert context["character_profiles"]["silas"].startswith("Имя: Сайлас")
        assert "Возраст: 400" in context["character_profiles"]["silas"]
        assert context["knowledge_journals"]["silas"] == ""
        assert "knowledge_firewall_v5" not in context
        assert "dialogue_frames" not in context
        assert "source_fact_ids" not in json.dumps(context["simple_knowledge_rules"], ensure_ascii=False)
        assert "own profile + own knowledge_journal" in context["scene_logic_guardrails"]["knowledge_causality"]["rule"]


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
