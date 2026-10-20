"""Port of generator-api's single-call, receipt-protected RAW persistence.

The large author's TXT is stored verbatim in Roman AI's existing v5 draft
format, with no extra GPT Action and no changes to ongoing game sessions.
"""
import hashlib
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import novel_drafts, storage
from app.main import novel_draft_intake_chunk_append, novel_draft_status_get
from app.models import NovelDraftIntakeChunk


def _setup(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()
    return novel_drafts.create_draft("full_raw_test", "Пока мир не сгорит", version=5)["draft_id"]


def _call(sid, text, **kwargs):
    return novel_draft_intake_chunk_append(
        sid,
        NovelDraftIntakeChunk(
            block_id="uploaded_novel_txt",
            stage="source",
            chunk_index=kwargs.pop("chunk_index", 0),
            raw_text=text,
            full_raw=kwargs.pop("full_raw", True),
            is_last=kwargs.pop("is_last", False),
            **kwargs,
        ),
    )


def test_generator_style_single_action_persists_large_exact_raw_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp)
        # Larger than both the static 6k Action chunk hint and the 100k
        # legacy per-chunk backend limit; preserve Unicode and whitespace.
        raw = ("\n Рината, Эдриан и Дантэ. Длинная биография, скрытые знания.  \n" * 4500) + "\nКОНЕЦ ФАЙЛА\n"
        assert len(raw) > 100_000

        result = _call(sid, raw)
        assert result["complete"] is True
        assert result["already_completed"] is False
        assert result["chunk_count"] == 1
        assert result["char_count"] == len(raw)
        assert result["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert result["draft_revision"] == 1

        draft = novel_drafts._read(sid)
        assert draft["sections"]["intake"]["blocks"][0]["raw_text"] == raw
        assert draft["intake_uploads"]["uploaded_novel_txt"]["completed"] is True

        status = novel_draft_status_get(sid)
        assert status["intake_uploads"]["pending_count"] == 0
        assert status["intake_uploads"]["completed"][0]["char_count"] == len(raw)
        assert status["revision"] == 1

        again = _call(sid, raw)
        assert again["complete"] is True
        assert again["already_completed"] is True
        assert novel_draft_status_get(sid)["revision"] == 1

        with pytest.raises(HTTPException) as exc:
            _call(sid, raw + "СЛУЧАЙНАЯ ПРАВКА")
        assert exc.value.status_code == 409
        assert novel_drafts._read(sid)["sections"]["intake"]["blocks"][0]["raw_text"] == raw


def test_existing_22657_character_pending_upload_can_be_finished_with_one_full_raw():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp)
        raw = ("Сохранённый исходный файл.\n" * 2500) + "ФИНАЛ"
        first = raw[:22657]
        first_response = _call(sid, first, full_raw=False, is_last=False)
        assert first_response["next_chunk_index"] == 1
        assert novel_draft_status_get(sid)["intake_uploads"]["pending_count"] == 1
        assert novel_draft_status_get(sid)["revision"] == 0

        # A mismatched prefix is rejected: no data loss, no duplicate.
        with pytest.raises(HTTPException) as exc:
            _call(sid, "ДРУГОЕ НАЧАЛО\n" + raw)
        assert exc.value.status_code == 409
        assert novel_draft_status_get(sid)["intake_uploads"]["pending_count"] == 1

        done = _call(sid, raw)
        assert done["complete"] is True
        assert done["char_count"] == len(raw)
        assert done["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        status = novel_draft_status_get(sid)
        assert status["revision"] == 1
        assert status["intake_uploads"]["pending_count"] == 0
        assert status["intake_uploads"]["completed"][0]["chunk_count"] == 1
        assert novel_drafts._read(sid)["sections"]["intake"]["blocks"][0]["raw_text"] == raw


def test_legacy_chunk_fallback_remains_supported_and_full_mode_requires_index_zero():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp)
        assert _call(sid, "ПЕРВАЯ", full_raw=False, is_last=False)["next_chunk_index"] == 1
        end = _call(sid, "ВТОРАЯ", full_raw=False, chunk_index=1, is_last=True)
        assert end["complete"] is True
        assert novel_drafts._read(sid)["sections"]["intake"]["blocks"][0]["raw_text"] == "ПЕРВАЯВТОРАЯ"

        another = novel_drafts.create_draft("incorrect_index", "Second", version=5)["draft_id"]
        with pytest.raises(HTTPException) as exc:
            _call(another, "УЖЕ ВСЁ", chunk_index=1)
        assert exc.value.status_code == 409
