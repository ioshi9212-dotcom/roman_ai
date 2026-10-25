"""Draft creation must not strand successful writes when an Actions reply is lost."""
import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from app import novel_drafts, storage
from app.main import novel_draft_create, novels_list
from app.models import NovelDraftCreate


def use_storage(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def request(key="novel-setup-attempt-1", title="Пока мир не сгорит", novel_id="until-world-burns"):
    return NovelDraftCreate(novel_id=novel_id, title=title, version=5, request_id=key)


def test_repeated_create_with_same_request_id_returns_original_draft_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        use_storage(tmp)
        first = novel_draft_create(request())
        assert first["draft_id"] and first["revision"] == 0
        assert first["novel_id"] == "until-world-burns"

        # Simulate interrupted setup, then a lost create response and a retry.
        draft_path = novel_drafts._draft_path(first["draft_id"])
        raw = json.loads(draft_path.read_text(encoding="utf-8"))
        raw["revision"] = 6
        raw["sections"] = {"novel": {"title": "Пока мир не сгорит"}}
        novel_drafts._write(draft_path, raw)

        replay = novel_draft_create(request())
        assert replay["draft_id"] == first["draft_id"]
        assert replay["revision"] == 6
        assert replay["saved_sections"] == ["novel"]
        assert len(list(novel_drafts._drafts_dir().glob("*.json"))) == 1


def test_same_idempotency_key_different_payload_is_rejected_without_any_new_draft():
    with tempfile.TemporaryDirectory() as tmp:
        use_storage(tmp)
        novel_draft_create(request())
        with pytest.raises(HTTPException) as exc:
            novel_draft_create(request(title="Другая история"))
        assert exc.value.status_code == 409
        assert exc.value.detail == "DRAFT_CREATE_REQUEST_CONFLICT"
        assert len(list(novel_drafts._drafts_dir().glob("*.json"))) == 1


def test_legacy_double_create_can_be_recovered_through_existing_listnovels_action():
    with tempfile.TemporaryDirectory() as tmp:
        use_storage(tmp)
        first = novel_draft_create(request(key=None))
        second = novel_draft_create(request(key=None))
        assert first["draft_id"] != second["draft_id"]
        # Existing unrelated novels and other drafts are not exposed as raw.
        novel_draft_create(request(key="other", title="Другая история"))
        for row in (first, second):
            path = novel_drafts._draft_path(row["draft_id"])
            draft = json.loads(path.read_text(encoding="utf-8"))
            draft["sections"] = {"intake": {"blocks": [{"raw_text": "СЕКРЕТНЫЙ ТЕКСТ"}]}}
            novel_drafts._write(path, draft)

        normal = novels_list()
        assert "drafts" not in normal
        recovered = novels_list(include_drafts=True, draft_title="Пока мир не сгорит")
        assert {r["draft_id"] for r in recovered["drafts"]} == {
            first["draft_id"], second["draft_id"]
        }
        assert all(r["raw_intake_present"] for r in recovered["drafts"])
        assert "СЕКРЕТНЫЙ ТЕКСТ" not in json.dumps(recovered, ensure_ascii=False)
        assert "Другая история" not in json.dumps(recovered, ensure_ascii=False)
        assert all("sections" not in row for row in recovered["drafts"])


def test_draft_recovery_requires_exact_title_to_prevent_enumeration():
    with pytest.raises(HTTPException) as exc:
        novels_list(include_drafts=True)
    assert exc.value.status_code == 422


def test_static_openapi_has_typed_draft_id_response_without_adding_action_31():
    schema = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text(encoding="utf-8"))
    all_ops = [(p, method, data["operationId"]) for p, obj in schema["paths"].items()
               for method, data in obj.items() if isinstance(data, dict) and "operationId" in data]
    assert len(all_ops) == 30
    create = schema["paths"]["/novel-drafts"]["post"]
    assert create["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/NovelDraftStatus")
    assert "draft_id" in schema["components"]["schemas"]["NovelDraftStatus"]["required"]
    assert "request_id" in schema["components"]["schemas"]["NovelDraftCreate"]["properties"]
    listed = schema["paths"]["/novels"]["get"]
    assert listed["operationId"] == "listNovels"
    assert {x["name"] for x in listed["parameters"]} == {"draft_title", "include_drafts"}
