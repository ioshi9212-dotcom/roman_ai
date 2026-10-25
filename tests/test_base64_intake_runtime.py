"""Byte-preserving RAW fallback learned from generator Novel AI.

Never finalize from an incomplete prefix, a guessed text, or only a checksum.
"""
import base64
import hashlib
import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from app import novel_drafts, storage
from app.main import novel_draft_intake_chunk_append
from app.models import NovelDraftIntakeChunk

LIMIT = 12288


def _storage(root):
    storage.DATA_DIR = Path(root)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return novel_drafts.create_draft("roman-b64", "ПОКА МИР НЕ СГОРИТ", 5)["draft_id"]


def _part(blob, i, *, digest=None, byte_count=None, block="original_bytes"):
    total = (len(blob) + LIMIT - 1) // LIMIT
    chunk = blob[i * LIMIT:(i+1) * LIMIT]
    return NovelDraftIntakeChunk(
        block_id=block, stage="setup", chunk_index=i, full_raw=False,
        total_parts=total,
        expected_bytes=len(blob) if byte_count is None else byte_count,
        expected_sha256=digest or hashlib.sha256(blob).hexdigest(),
        chunk_b64=base64.b64encode(chunk).decode("ascii"),
    )


def test_97094_original_bytes_are_persisted_losslessly_with_sha_and_replays():
    prefix = "Пока мир не сгорит\r\nРината Дейл, Данте, Сайлас. «Блять».\n".encode("utf-8")
    blob = prefix + b"z" * (97094 - len(prefix))
    assert len(blob) == 97094
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        did = _novel()
        total = (len(blob) + LIMIT - 1) // LIMIT
        for i in range(total):
            response = novel_draft_intake_chunk_append(did, _part(blob, i))
            assert response["draft_id"] == did
            assert response["complete"] == (i == total - 1)
            if i < total - 1:
                assert response["next_chunk_index"] == i + 1
                assert novel_drafts._read(did)["revision"] == 0
                # Retry a received part without advancing its progress:
                prior = novel_draft_intake_chunk_append(did, _part(blob, i))
                assert prior["already_received"] is True
                assert prior["next_chunk_index"] == i + 1
                status = novel_drafts.draft_status(did)
                pending = status["intake_uploads"]["pending"][0]
                assert pending["next_chunk_index"] == i + 1
                assert pending["transfer"] == "base64"
            else:
                assert response["byte_count"] == 97094
                assert response["raw_sha256"] == hashlib.sha256(blob).hexdigest()
                assert response["char_count"] == len(blob.decode("utf-8"))
        result = novel_drafts._read(did)
        assert result["revision"] == 1
        assert result["sections"]["intake"]["blocks"][0]["raw_text"].encode("utf-8") == blob
        assert novel_drafts.draft_status(did)["intake_uploads"]["pending_count"] == 0
        again = novel_draft_intake_chunk_append(did, _part(blob, total - 1))
        assert again["already_completed"] is True
        assert again["raw_sha256"] == hashlib.sha256(blob).hexdigest()


def test_existing_prefix_is_preserved_and_full_source_saved_under_new_block():
    content = ("Сайлас: «Не зови меня так».\n" * 2400).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        did = _novel()
        prefix = content[:9000].decode("utf-8", errors="ignore")
        novel_draft_intake_chunk_append(did, NovelDraftIntakeChunk(
            block_id="old_partial", stage="setup", chunk_index=0,
            raw_text=prefix, full_raw=True))
        for i in range((len(content) + LIMIT - 1) // LIMIT):
            done = novel_draft_intake_chunk_append(did, _part(content, i, block="full_original"))
        assert done["complete"]
        stored = novel_drafts._read(did)["sections"]["intake"]["blocks"]
        assert [x["block_id"] for x in stored] == ["old_partial", "full_original"]
        assert stored[0]["raw_text"] == prefix
        assert stored[1]["raw_text"].encode("utf-8") == content


def test_wrong_whole_sha_blocks_commit_and_keeps_prefix_resume_data():
    blob = ("🤍 Рината.\r\n" * 1600).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        did = _novel()
        total = (len(blob) + LIMIT - 1) // LIMIT
        for i in range(total - 1):
            novel_draft_intake_chunk_append(did, _part(blob, i, digest="a" * 64))
        with pytest.raises(HTTPException) as exc:
            novel_draft_intake_chunk_append(did, _part(blob, total - 1, digest="a" * 64))
        assert exc.value.status_code == 409
        assert "RAW_BASE64_SHA256_MISMATCH" in str(exc.value.detail)
        row = novel_drafts._read(did)
        assert row["revision"] == 0
        assert not row["sections"]
        assert novel_drafts.draft_status(did)["intake_uploads"]["pending_count"] == 1


def test_conflicting_retries_or_corrupted_chunk_refused():
    blob = b"x" * (LIMIT + 500)
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        did = _novel()
        novel_draft_intake_chunk_append(did, _part(blob, 0))
        changed = _part(blob, 0)
        changed.chunk_b64 = base64.b64encode(b"y" * LIMIT).decode("ascii")
        with pytest.raises(HTTPException) as exc:
            novel_draft_intake_chunk_append(did, changed)
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException):
            novel_draft_intake_chunk_append(did, _part(blob, 0, byte_count=len(blob)+5))
        assert novel_drafts._read(did)["revision"] == 0


def test_base64_transport_requires_original_bytes_provenance_and_no_mixed_modes():
    with pytest.raises(ValidationError):
        NovelDraftIntakeChunk(block_id="raw", stage="setup", chunk_index=0,
                              raw_text="Invented source", chunk_b64="eA==",
                              expected_bytes=1, expected_sha256="0"*64, total_parts=1)
    with pytest.raises(ValidationError):
        NovelDraftIntakeChunk(block_id="raw", stage="setup", chunk_index=0,
                              chunk_b64="eA==", expected_bytes=1, total_parts=1)
    with tempfile.TemporaryDirectory() as tmp:
        _storage(tmp)
        did = _novel()
        wrong = NovelDraftIntakeChunk(block_id="b64", stage="setup", chunk_index=0,
                          chunk_b64="not-base64!", expected_bytes=2, total_parts=1,
                          expected_sha256="0"*64)
        with pytest.raises(HTTPException) as exc:
            novel_draft_intake_chunk_append(did, wrong)
        assert exc.value.status_code == 409
        assert novel_drafts._read(did)["revision"] == 0


def test_schema_has_30_actions_and_verified_fallback_inputs():
    root = Path(__file__).parents[1]
    schema = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    ops = [v["operationId"] for path in schema["paths"].values()
           for v in path.values() if isinstance(v, dict) and "operationId" in v]
    assert len(ops) == 30
    req = schema["components"]["schemas"]["NovelDraftIntakeChunk"]
    assert {"chunk_b64", "total_parts", "expected_bytes", "expected_sha256"} <= set(req["properties"])
    assert "chunk_b64" not in req["required"]
    txt = (root / "gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "chunk_b64" in txt
    assert len(txt) + txt.count("\n") < 8000
