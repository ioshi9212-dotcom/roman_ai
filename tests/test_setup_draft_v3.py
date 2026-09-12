import json
import tempfile
from pathlib import Path

import pytest

from app import draft_intake_runtime, novel_drafts, storage
from app.novel_access import get_novel_read_chunk
from app.setup_draft_v3_runtime import confirm_reconciliation, set_launch_state


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def foundation():
    return {
        "facts": [
            {
                "fact_id": "f_pov",
                "text": "Рина работает в архиве и боится воды.",
                "source": "user_setup",
                "stored_in": ["characters[rina]"],
                "story_use": "reference",
            },
            {
                "fact_id": "f_world",
                "text": "Город живёт вокруг закрытого архива.",
                "source": "user_setup",
                "stored_in": ["lore.city"],
                "story_use": "hook",
            },
        ],
        "hooks": [
            {
                "hook_id": "h_world",
                "fact_ids": ["f_world"],
                "condition": "когда тайна архива становится уместна",
                "status": "latent",
            }
        ],
        "story_pillars": [
            {
                "pillar_id": "archive_mystery",
                "label": "тайна архива",
                "source_fact_ids": ["f_world"],
            }
        ],
    }


def save_content(draft_id: str):
    novel_drafts.save_section(
        draft_id,
        "novel",
        json.dumps({"pov_character": "rina", "genres": ["триллер"], "age_rating": "18+"}, ensure_ascii=False),
    )
    novel_drafts.save_section(
        draft_id,
        "characters",
        json.dumps(
            [
                {"character_id": "rina", "name": "Рина", "is_pov": True, "job": "архивист"},
                {"character_id": "adrian", "name": "Адриан"},
            ],
            ensure_ascii=False,
        ),
    )
    novel_drafts.save_section(
        draft_id,
        "lore",
        json.dumps({"city": "Город живёт вокруг закрытого архива."}, ensure_ascii=False),
    )
    novel_drafts.save_section(draft_id, "foundation", json.dumps(foundation(), ensure_ascii=False))


def read_current_revision(draft_id: str):
    manifest = novel_drafts.prepare_draft_read(draft_id)
    for index in range(manifest["chunk_count"]):
        get_novel_read_chunk(manifest["read_id"], index)
    return novel_drafts.draft_status(draft_id)


def prepare_reconciled_v3(draft_id: str):
    raw = "Рина работает в архиве и боится воды. Город живёт вокруг закрытого архива."
    draft_intake_runtime.append_intake_chunk(
        draft_id,
        block_id="user_001",
        stage="setup_user_message",
        chunk_index=0,
        raw_text=raw,
        is_last=True,
    )
    save_content(draft_id)
    revision = novel_drafts.draft_status(draft_id)["revision"]
    draft_intake_runtime.update_intake_mapping(
        draft_id,
        "user_001",
        fact_ids=["f_pov", "f_world"],
        reviewed_against_raw=True,
        contains_no_facts=False,
        replace=True,
        expected_revision=revision,
    )
    status = read_current_revision(draft_id)
    assert status["finalize_blocker"] == "RECONCILIATION_REQUIRED"
    revision = status["revision"]
    return confirm_reconciliation(
        draft_id,
        expected_revision=revision,
        confirmed_against_raw=True,
        unresolved_conflicts=[],
    )


def test_v3_content_can_finalize_without_starting_state_then_wait_for_launch():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = novel_drafts.create_draft("v3_setup", "V3 Setup", version=3)["draft_id"]
        status = prepare_reconciled_v3(draft_id)

        assert status["ready_to_finalize"] is True
        assert "starting_state" not in status["missing_required_sections"]
        final = novel_drafts.finalize_draft(draft_id)
        assert final["content_finalized"] is True
        assert final["awaiting_launch_start"] is True
        assert final["session_ready"] is False

        with pytest.raises(RuntimeError, match="LAUNCH_STATE_REQUIRED"):
            novel_drafts.create_session_from_draft(draft_id)


def test_v3_launch_state_is_separate_and_creates_turn_zero_session_only_after_launch():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = novel_drafts.create_draft("v3_launch", "V3 Launch", version=3)["draft_id"]
        prepare_reconciled_v3(draft_id)
        novel_drafts.finalize_draft(draft_id)
        status = novel_drafts.draft_status(draft_id)

        launched = set_launch_state(
            draft_id,
            expected_finalized_revision=status["revision"],
            starting_state_json=json.dumps(
                {
                    "pov": "Рина",
                    "current": {
                        "location": "архив",
                        "scene": "первый рабочий день после закрытия",
                        "present_characters": ["Рина", "Адриан"],
                    },
                },
                ensure_ascii=False,
            ),
            launch_hint="Начать в архиве.",
        )
        assert launched["launch_state_ready"] is True
        assert launched["session_ready"] is True

        meta = novel_drafts.create_session_from_draft(draft_id)
        root = storage.SESSIONS_DIR / meta["session_id"]
        state = storage._read_json(root / "state.json", {})
        assert meta["turn_number"] == 0
        assert state["current"]["location"] == "архив"
        assert state["current"]["present_characters"] == ["rina", "adrian"]


def test_v3_any_content_write_invalidates_full_read_and_reconciliation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = novel_drafts.create_draft("v3_rewrite", "V3 Rewrite", version=3)["draft_id"]
        prepare_reconciled_v3(draft_id)
        before = novel_drafts.draft_status(draft_id)
        assert before["reconciliation_current"] is True

        novel_drafts.save_section(
            draft_id,
            "novel",
            json.dumps({"pov_character": "rina", "genres": ["триллер", "драма"], "age_rating": "18+"}, ensure_ascii=False),
        )
        after = novel_drafts.draft_status(draft_id)
        assert after["reconciliation_current"] is False
        assert after["ready_to_finalize"] is False
        assert after["finalize_blocker"] == "INTAKE_FINAL_READ_REQUIRED"


def test_v3_mapping_replace_can_remove_wrong_fact_without_changing_raw():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = novel_drafts.create_draft("v3_mapping", "V3 Mapping", version=3)["draft_id"]
        raw = "Рина боится воды."
        draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup_user_message", chunk_index=0, raw_text=raw, is_last=True
        )
        save_content(draft_id)

        rev = novel_drafts.draft_status(draft_id)["revision"]
        draft_intake_runtime.update_intake_mapping(
            draft_id,
            "b1",
            fact_ids=["f_pov", "f_world"],
            reviewed_against_raw=True,
            contains_no_facts=False,
            replace=True,
            expected_revision=rev,
        )
        rev = novel_drafts.draft_status(draft_id)["revision"]
        draft_intake_runtime.update_intake_mapping(
            draft_id,
            "b1",
            fact_ids=["f_pov"],
            reviewed_against_raw=True,
            contains_no_facts=False,
            replace=True,
            expected_revision=rev,
        )

        block = novel_drafts._read(draft_id)["sections"]["intake"]["blocks"][0]
        assert block["raw_text"] == raw
        assert block["fact_ids"] == ["f_pov"]


def test_reconciliation_rejects_unresolved_semantic_conflicts():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = novel_drafts.create_draft("v3_conflict", "V3 Conflict", version=3)["draft_id"]
        raw = "Рине 24 года."
        draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="b1", stage="setup_user_message", chunk_index=0, raw_text=raw, is_last=True
        )
        save_content(draft_id)
        rev = novel_drafts.draft_status(draft_id)["revision"]
        draft_intake_runtime.update_intake_mapping(
            draft_id,
            "b1",
            fact_ids=["f_pov"],
            reviewed_against_raw=True,
            contains_no_facts=False,
            replace=True,
            expected_revision=rev,
        )
        status = read_current_revision(draft_id)
        with pytest.raises(ValueError, match="RECONCILIATION_CONFLICTS_REMAIN"):
            confirm_reconciliation(
                draft_id,
                expected_revision=status["revision"],
                confirmed_against_raw=True,
                unresolved_conflicts=["Возраст Рины противоречит другой части анкеты."],
            )
