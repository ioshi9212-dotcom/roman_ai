"""One-call verbatim TXT transfer via the documented GPT Actions openaiFileIdRefs."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import novel_drafts, storage
from app.main import app, novel_draft_intake_chunk_append
from app.models import NovelDraftIntakeChunk


URL = "https://files.oaiusercontent.com/file-abc123?se=shortlived&sig=secret"
REF = {"id": "file-abc123", "name": "Пока мир не сгорит.txt",
       "mime_type": "text/plain", "download_link": URL}
REGIONAL_URL = (
    "https://sdmntprnortheu.oaiusercontent.com/files/"
    "00000000-0000-0000-0000-000000000001/raw?se=shortlived&sig=TEST_ONLY"
)


class Response:
    status = 200
    def __init__(self, data):
        self.data = data
        self.headers = {"Content-Length": str(len(data))}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, count):
        return self.data[:count]


class Opener:
    def __init__(self, data, url=URL):
        self.data = data
        self.url = url
    def open(self, request, timeout=None):
        assert request.full_url == self.url
        assert timeout <= 15
        return Response(self.data)


def _setup(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _body(ref=None, block="complete_source"):
    return NovelDraftIntakeChunk(
        block_id=block, stage="setup", chunk_index=0,
        full_raw=True, openaiFileIdRefs=[REF if ref is None else ref],
    )


def test_attach_txt_saves_exact_unicode_and_proves_sha_without_model_transcription():
    raw = ("Рина: «Блять, не смягчай».  \r\nДанте\nСайлас.\n") * 1500
    data = raw.encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        did = novel_drafts.create_draft("story", "ПОКА МИР НЕ СГОРИТ", 5)["draft_id"]
        with patch("app.gpt_file_refs.build_opener", return_value=Opener(data)):
            saved = novel_draft_intake_chunk_append(did, _body())
            replay = novel_draft_intake_chunk_append(did, _body())
        assert saved["complete"] is True and saved["char_count"] == len(raw)
        assert saved["raw_sha256"] == hashlib.sha256(data).hexdigest()
        assert replay["already_completed"] is True
        assert replay["raw_sha256"] == saved["raw_sha256"]
        d = novel_drafts._read(did)
        assert d["sections"]["intake"]["blocks"][0]["raw_text"] == raw
        assert d["revision"] == 1


def test_attached_full_source_preserves_prior_completed_prefix_as_separate_block():
    raw = "Дантэ и Эдриан.\n" * 3600
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        did = novel_drafts.create_draft("story", "ПОКА МИР НЕ СГОРИТ", 5)["draft_id"]
        prefix = raw[:9227]
        novel_draft_intake_chunk_append(did, NovelDraftIntakeChunk(
            block_id="partial_old", stage="setup", chunk_index=0,
            raw_text=prefix, full_raw=True,
        ))
        with patch("app.gpt_file_refs.build_opener", return_value=Opener(raw.encode("utf-8"))):
            saved = novel_draft_intake_chunk_append(did, _body(block="full_user_txt"))
        assert saved["char_count"] == len(raw)
        blocks = novel_drafts._read(did)["sections"]["intake"]["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["raw_text"] == prefix
        assert blocks[1]["raw_text"] == raw


@pytest.mark.parametrize("url", [
    REGIONAL_URL,
    REGIONAL_URL.replace("sdmntprnortheu", "sdmntpreastus2"),
])
def test_regional_txt_import_preserves_existing_profiles_and_exact_bytes(url):
    raw = "\ufeff  Исходный канон.\r\nНе смягчай формулировки.  \n" * 1700
    binary = raw.encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        did = novel_drafts.create_draft("regional-source", "Проверка импорта", 5)["draft_id"]
        novel_drafts.save_section(
            did, "characters", json.dumps([
                {"character_id": "rina", "name": "Рина", "is_pov": True},
            ], ensure_ascii=False), expected_revision=0,
        )
        before = novel_drafts._read(did)
        payload = {
            "block_id": "original_txt", "stage": "source", "chunk_index": 0,
            "openaiFileIdRefs": [{**REF, "download_link": url}],
            "full_raw": True, "is_last": False,
        }
        with TestClient(app) as client, patch(
            "app.gpt_file_refs.build_opener", return_value=Opener(binary, url),
        ):
            response = client.post(f"/novel-drafts/{did}/intake/chunks", json=payload)
            assert response.status_code == 200
            saved = response.json()
            replay = client.post(f"/novel-drafts/{did}/intake/chunks", json=payload)
            # The screenshot's empty-ref retry still must not write empty RAW.
            empty = client.post(f"/novel-drafts/{did}/intake/chunks", json={
                **payload, "openaiFileIdRefs": [],
            })
        assert saved["complete"] is True
        assert saved["raw_sha256"] == hashlib.sha256(binary).hexdigest()
        assert saved["char_count"] == len(raw)
        assert replay.status_code == 200 and replay.json()["already_completed"] is True
        assert empty.status_code == 422
        after = novel_drafts._read(did)
        assert after["sections"]["characters"] == before["sections"]["characters"]
        assert after["sections"]["intake"]["blocks"][0]["raw_text"] == raw
        assert after["revision"] == before["revision"] + 1


@pytest.mark.parametrize("url", [
    "http://files.oaiusercontent.com/file-x",
    "https://files.oaiusercontent.com.evil.test/file-x",
    "https://127.0.0.1/file-x",
    "https://files.oaiusercontent.com:8443/file-x",
    "https://user:pw@files.oaiusercontent.com/file-x",
    "https://files.oaiusercontent.com/file-x#fragment",
    "http://sdmntprnortheu.oaiusercontent.com/files/file-x/raw",
    "https://sdmntprnortheu.oaiusercontent.com.evil.test/file-x",
    "https://evil.sdmntprnortheu.oaiusercontent.com/file-x",
    "https://sdmntprnortheu-oaiusercontent.com/file-x",
    "https://sdmntprnortheu.oaiusercontent.com:8443/file-x",
    "https://user:pw@sdmntprnortheu.oaiusercontent.com/file-x",
    "https://sdmntprnortheu.oaiusercontent.com/file-x#fragment",
    "https://other.oaiusercontent.com/file-x",
    "https://sdmntpr.oaiusercontent.com/file-x",
])
def test_download_blocks_unsafe_urls_without_network(url):
    ref = {**REF, "download_link": url}
    with patch("app.gpt_file_refs.build_opener") as network, pytest.raises(HTTPException) as exc:
        from app.gpt_file_refs import read_gpt_txt_file_refs
        read_gpt_txt_file_refs([ref])
    assert exc.value.status_code == 422
    network.assert_not_called()


def test_local_sandbox_file_ref_provides_specific_recovery_code_without_leaking_path():
    from app.gpt_file_refs import read_gpt_txt_file_refs
    for local in ("/mnt/data/private setup.txt", "sandbox:/mnt/data/private setup.txt",
                  "file:///mnt/data/private setup.txt"):
        with pytest.raises(HTTPException) as err:
            read_gpt_txt_file_refs([{**REF, "download_link": local}])
        assert err.value.status_code == 422
        detail = err.value.detail
        assert detail["code"] == "GPT_FILE_REF_LOCAL_PATH"
        assert detail["link_kind"] == "local_sandbox_path"
        assert local not in str(detail)
        assert "raw_text" in detail["message"]


def test_unapproved_host_is_diagnosed_without_leaking_signed_url():
    from app.gpt_file_refs import read_gpt_txt_file_refs
    sensitive = "https://another.example.org/private/secret.txt?sig=VERY_PRIVATE_TOKEN"
    with pytest.raises(HTTPException) as err:
        read_gpt_txt_file_refs([{**REF, "download_link": sensitive}])
    detail = err.value.detail
    assert detail["code"] == "GPT_FILE_REF_UNSUPPORTED_OR_UNSAFE_URL"
    assert detail["received_host"] == "another.example.org"
    assert detail["expected_host"] == "files.oaiusercontent.com"
    assert "VERY_PRIVATE_TOKEN" not in str(detail)
    assert "/private/secret.txt" not in str(detail)


def test_missing_file_or_mixed_raw_rejected_before_any_download():
    with pytest.raises(ValidationError):
        NovelDraftIntakeChunk(block_id="a",stage="setup",chunk_index=0)
    with pytest.raises(ValidationError):
        NovelDraftIntakeChunk(block_id="a",stage="setup",chunk_index=0,
            full_raw=True,raw_text="duplicate",openaiFileIdRefs=[REF])
    with pytest.raises(ValidationError):
        NovelDraftIntakeChunk(block_id="a",stage="setup",chunk_index=1,
            full_raw=True,openaiFileIdRefs=[REF])


def test_bad_utf8_does_not_mutate_draft():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        did = novel_drafts.create_draft("bad", "Test", 5)["draft_id"]
        with patch("app.gpt_file_refs.build_opener", return_value=Opener(b"\xff\xfe")):
            with pytest.raises(HTTPException) as exc:
                novel_draft_intake_chunk_append(did, _body())
        assert exc.value.status_code == 422
        assert novel_drafts._read(did)["revision"] == 0


def test_openapi_preserves_30_actions_and_declares_file_reference_field():
    root = Path(__file__).parents[1]
    y = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    ops = [row.get("operationId") for methods in y["paths"].values()
           for row in methods.values() if isinstance(row, dict) and row.get("operationId")]
    assert len(ops) == 30
    schema = y["components"]["schemas"]["NovelDraftIntakeChunk"]
    assert "openaiFileIdRefs" in schema["properties"]
    assert schema["properties"]["openaiFileIdRefs"]["items"]["type"] == "string"
    assert "raw_text" not in schema["required"]
    instr = (root / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "openaiFileIdRefs" in instr
    assert len(instr) + instr.count("\n") < 8000
