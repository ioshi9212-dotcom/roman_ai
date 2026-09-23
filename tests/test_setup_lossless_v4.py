import json
import tempfile
from pathlib import Path

import pytest

from app import draft_intake_runtime, novel_drafts, storage


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _save_v4(draft_id: str, section: str, value):
    revision = novel_drafts.draft_status(draft_id)["revision"]
    return novel_drafts.save_section(
        draft_id,
        section,
        json.dumps(value, ensure_ascii=False),
        expected_revision=revision,
    )


def test_v4_coverage_detects_each_omitted_character_detail_not_just_the_block():
    draft = {
        "version": 4,
        "sections": {
            "intake": {
                "version": 5,
                "blocks": [{
                    "block_id": "char_rina",
                    "stage": "character",
                    "raw_text": (
                        "Внешность:\n"
                        "Высокая, серые глаза, короткие чёрные волосы.\n"
                        "Привычки:\n"
                        "Когда нервничает, крутит кольцо на пальце."
                    ),
                    "fact_ids": ["f_appearance"],
                    "reviewed_against_raw": True,
                    "contains_no_facts": False,
                }],
            },
            "foundation": {
                "facts": [{
                    "fact_id": "f_appearance",
                    "text": "Рина высокая, с серыми глазами и короткими чёрными волосами.",
                    "stored_in": ["characters[rina].appearance"],
                    "story_use": "reference",
                    "source_unit_ids": ["char_rina:u0001"],
                }],
                "hooks": [],
                "story_pillars": [{"pillar_id": "p", "label": "Рина", "source_fact_ids": ["f_appearance"]}],
            },
        },
    }
    coverage = draft_intake_runtime._coverage(draft)
    assert coverage["lossless_detail_coverage_required"] is True
    assert coverage["source_unit_count"] == 2
    assert coverage["covered_source_unit_count"] == 1
    assert coverage["ok"] is False
    assert coverage["uncovered_source_units"][0]["source_unit_id"] == "char_rina:u0002"
    assert "крутит кольцо" in coverage["uncovered_source_units"][0]["text"]


def test_v4_cannot_mark_block_reviewed_until_all_source_units_are_mapped():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = novel_drafts.create_draft("lossless_map", "Lossless", version=4)["draft_id"]
        result = draft_intake_runtime.append_intake_chunk(
            draft_id,
            block_id="char_rina",
            stage="character",
            chunk_index=0,
            raw_text="У Рины серые глаза. Когда нервничает, она крутит кольцо.",
            is_last=True,
        )
        assert [row["source_unit_id"] for row in result["source_units"]] == [
            "char_rina:u0001",
            "char_rina:u0002",
        ]

        _save_v4(draft_id, "foundation", {
            "facts": [{
                "fact_id": "f_eyes",
                "text": "У Рины серые глаза.",
                "stored_in": ["characters[rina].appearance"],
                "story_use": "reference",
                "source_unit_ids": ["char_rina:u0001"],
            }],
            "hooks": [],
            "story_pillars": [{"pillar_id": "p", "label": "Рина", "source_fact_ids": ["f_eyes"]}],
        })

        with pytest.raises(ValueError, match="INTAKE_SOURCE_UNITS_UNCOVERED:char_rina:u0002"):
            draft_intake_runtime.update_intake_mapping(
                draft_id,
                "char_rina",
                fact_ids=["f_eyes"],
                reviewed_against_raw=True,
                contains_no_facts=False,
            )


def test_v4_exact_raw_details_are_sealed_into_foundation_and_character_card():
    raw = (
        "Внешность:\n"
        "У Рины серые глаза и короткие чёрные волосы.\n"
        "Привычки:\n"
        "Когда нервничает, крутит кольцо."
    )
    draft = {
        "version": 4,
        "sections": {
            "intake": {
                "version": 5,
                "blocks": [{
                    "block_id": "rina",
                    "stage": "character",
                    "raw_text": raw,
                    "fact_ids": ["f_appearance", "f_habit"],
                    "reviewed_against_raw": True,
                    "contains_no_facts": False,
                }],
            }
        },
    }
    template = {
        "version": 4,
        "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
        "foundation": {
            "facts": [
                {
                    "fact_id": "f_appearance",
                    "text": "У Рины серые глаза и короткие чёрные волосы.",
                    "stored_in": ["characters[rina].appearance"],
                    "story_use": "reference",
                    "source_unit_ids": ["rina:u0001"],
                },
                {
                    "fact_id": "f_habit",
                    "text": "Рина крутит кольцо, когда нервничает.",
                    "stored_in": ["characters[rina].habits"],
                    "story_use": "reference",
                    "source_unit_ids": ["rina:u0002"],
                },
            ],
            "hooks": [],
            "story_pillars": [{"pillar_id": "p", "label": "Рина", "source_fact_ids": ["f_appearance", "f_habit"]}],
        },
    }
    result = draft_intake_runtime.enrich_template_with_source_evidence(draft, template)
    assert result["setup_integrity"]["all_source_units_preserved"] is True
    card = result["characters"][0]
    details = {row["source_unit_id"]: row for row in card["setup_details"]}
    assert details["rina:u0001"]["text"] == "У Рины серые глаза и короткие чёрные волосы."
    assert details["rina:u0001"]["category"] == "appearance"
    assert details["rina:u0002"]["text"] == "Когда нервничает, крутит кольцо."
    assert details["rina:u0002"]["category"] == "habits"

    facts = {row["fact_id"]: row for row in result["foundation"]["facts"]}
    assert facts["f_habit"]["source_evidence"][0]["text"] == "Когда нервничает, крутит кольцо."


def test_v4_hook_source_text_is_preserved_exactly_in_canonical_foundation():
    draft = {
        "version": 4,
        "sections": {
            "intake": {
                "version": 5,
                "blocks": [{
                    "block_id": "plot",
                    "stage": "novel",
                    "raw_text": "Крючок: каждую осень неизвестный оставляет у двери Рины красную нить.",
                    "fact_ids": ["f_hook"],
                    "reviewed_against_raw": True,
                    "contains_no_facts": False,
                }],
            }
        },
    }
    template = {
        "version": 4,
        "characters": [{"character_id": "rina", "name": "Рина", "is_pov": True}],
        "foundation": {
            "facts": [{
                "fact_id": "f_hook",
                "text": "Неизвестный ежегодно оставляет красную нить.",
                "stored_in": ["novel.hooks"],
                "story_use": "hook",
                "source_unit_ids": ["plot:u0001"],
            }],
            "hooks": [{"hook_id": "red_thread", "fact_ids": ["f_hook"], "status": "latent"}],
            "story_pillars": [{"pillar_id": "mystery", "label": "Красная нить", "source_fact_ids": ["f_hook"]}],
        },
    }
    result = draft_intake_runtime.enrich_template_with_source_evidence(draft, template)
    evidence = result["foundation"]["facts"][0]["source_evidence"][0]
    assert evidence["text"] == "Крючок: каждую осень неизвестный оставляет у двери Рины красную нить."
