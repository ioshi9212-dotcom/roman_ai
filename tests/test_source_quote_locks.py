"""Every source-quoted field must remain exactly as written by the author."""
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import draft_intake_runtime, novel_drafts, storage
from app.main import novel_draft_section_save
from app.models import NovelDraftSection


def _setup(folder):
    storage.DATA_DIR = Path(folder)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()
    did = novel_drafts.create_draft("verbatim-roman", "ПОКА МИР НЕ СГОРИТ", 5)["draft_id"]
    original = "Рината: Дантэ охуенно ревнивый,  но не признаётся.\n\nСайлас: всё, что сказано, помни."
    result = draft_intake_runtime.save_complete_intake_raw(
        did, block_id="whole_raw", stage="setup", raw_text=original,
    )
    return did, original, result["draft_revision"]


def _save(did, section, data, revision, quotes=None):
    return novel_draft_section_save(did, NovelDraftSection(
        section_name=section, section_json=json.dumps(data, ensure_ascii=False),
        expected_revision=revision, source_quotes=quotes or {},
    ))


def test_author_character_wording_verbatim_and_generated_details_are_separate():
    with tempfile.TemporaryDirectory() as folder:
        _setup_path = Path(folder)
        did, raw, rev = _setup(_setup_path)
        phrase = raw.split("\n")[0]
        card = {
            "character_id": "rinata", "name": "Рината", "character": phrase,
            "generated_details": {"habit": "Крутит кольцо"},
        }
        result = _save(did, "characters", [card], rev, {"/0/character": phrase})
        saved = novel_drafts._read(did)
        assert saved["sections"]["characters"][0]["character"] == phrase
        assert saved["verbatim_locks"]["characters"]["/0/character"] == phrase
        assert saved["sections"]["characters"][0]["generated_details"]["habit"] == "Крутит кольцо"

        card["generated_details"]["habit"] = "Поправляет волосы"
        update = _save(did, "characters", [card], result["draft_revision"])
        assert update["section_changed"] is True
        assert novel_drafts._read(did)["verbatim_locks"]["characters"]["/0/character"] == phrase

        card["character"] = "Рината ревнива, но не признаётся."
        with pytest.raises(HTTPException) as error:
            _save(did, "characters", [card], update["draft_revision"])
        assert error.value.status_code == 422
        assert "CANON_LOCK_CHANGED" in str(error.value.detail)
        assert novel_drafts._read(did)["sections"]["characters"][0]["character"] == phrase


def test_fake_quote_and_softened_source_are_refused_without_revision_change():
    with tempfile.TemporaryDirectory() as folder:
        did, raw, rev = _setup(folder)
        phrase = raw.split("\n")[0]
        softened = phrase.replace("охуенно", "очень")
        card = {"character_id": "rinata", "name": "Рината", "character": softened}
        for quote, expected in [(phrase, "CANON_QUOTE_NOT_VERBATIM"), (softened, "CANON_QUOTE_MISSING_FROM_RAW")]:
            with pytest.raises(HTTPException) as error:
                _save(did, "characters", [card], rev, {"/0/character": quote})
            assert expected in str(error.value.detail)
        assert novel_drafts._read(did)["revision"] == rev
        assert "characters" not in novel_drafts._read(did)["sections"]
