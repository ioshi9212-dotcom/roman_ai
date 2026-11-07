"""Reviewed v5 blocks must remain readable during later reviews and uploads."""
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import main, novel_drafts, storage
from test_simple_profile_flow import _build_simple_draft


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    with TestClient(main.app) as value:
        yield value


def test_v5_review_is_idempotent_and_rejects_stale_revision(client):
    draft = client.post("/novel-drafts", json={"novel_id": "v5-review", "title": "Review", "version": 5}).json()
    did = draft["draft_id"]
    base = f"/novel-drafts/{did}"
    raw = "Согласованные персонажи и правила романа."
    saved = client.post(base + "/intake/chunks", json={
        "block_id": "assistant-proposal-01", "stage": "source", "chunk_index": 0,
        "raw_text": raw, "is_last": True,
    })
    assert saved.status_code == 200, saved.text
    revision = novel_drafts.draft_status(did)["revision"]
    body = {"fact_ids": [], "reviewed_against_raw": True,
            "contains_no_facts": False, "expected_revision": revision}
    response = client.post(base + "/intake/assistant-proposal-01/mapping", json=body)
    assert response.status_code == 200, response.text
    reviewed = response.json()
    assert reviewed["intake_coverage"]["ok"] is True
    assert reviewed["revision"] == revision + 1
    block = novel_drafts._read(did)["sections"]["intake"]["blocks"][0]
    assert block["raw_text"] == raw
    assert block["fact_ids"] == []
    assert block["contains_no_facts"] is False
    stale = client.post(base + "/intake/assistant-proposal-01/mapping", json=body)
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]
    body["expected_revision"] = reviewed["revision"]
    repeat = client.post(base + "/intake/assistant-proposal-01/mapping", json=body)
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["revision"] == reviewed["revision"]


def test_v4_review_still_requires_foundation_ids(client):
    draft = novel_drafts.create_draft("v4-review", "Review", version=4)
    response = client.post(f"/novel-drafts/{draft['draft_id']}/intake/raw/mapping", json={
        "fact_ids": [], "reviewed_against_raw": True, "contains_no_facts": False,
    })
    assert response.status_code == 409
    assert "must map to at least one" in response.json()["detail"]


def test_four_blocks_can_be_uploaded_reviewed_and_finalized_in_place(client):
    did = _build_simple_draft()
    base = f"/novel-drafts/{did}"
    originals = {"raw_setup": novel_drafts._read(did)["sections"]["intake"]["blocks"][0]["raw_text"]}
    # The first block is already reviewed. New uploads must preserve it.
    for index, bid in enumerate(["assistant-proposal-01", "user-02", "user-03"]):
        raw = "Дополнительные NPC создаются по ходу романа. POV не мебель. " + bid
        originals[bid] = raw
        body = {
            "block_id": bid, "stage": "source", "chunk_index": 0,
            "raw_text": raw, "is_last": True, "full_raw": index == 1,
        }
        if index == 2:
            blob = raw.encode("utf-8")
            body = {"block_id": bid, "stage": "source", "chunk_index": 0,
                    "chunk_b64": base64.b64encode(blob).decode("ascii"), "total_parts": 1,
                    "expected_bytes": len(blob), "expected_sha256": hashlib.sha256(blob).hexdigest()}
        response = client.post(base + "/intake/chunks", json=body)
        assert response.status_code == 200, response.text
    status = novel_drafts.draft_status(did)
    assert status["intake_coverage"]["block_count"] == 4
    assert status["intake_coverage"]["reviewed_blocks"] == 1
    assert status["finalize_blocker"] == "INTAKE_REVIEW_INCOMPLETE"
    assert client.post(base + "/finalize").status_code == 409
    for bid in list(originals)[1:]:
        revision = novel_drafts.draft_status(did)["revision"]
        response = client.post(base + f"/intake/{bid}/mapping", json={
            "fact_ids": [], "reviewed_against_raw": True, "contains_no_facts": False,
            "expected_revision": revision,
        })
        assert response.status_code == 200, response.text
    status = novel_drafts.draft_status(did)
    assert status["intake_coverage"]["reviewed_blocks"] == 4
    assert status["finalize_blocker"] == "INTAKE_FINAL_READ_REQUIRED"
    manifest = client.post(base + "/read").json()
    for index in range(manifest["chunk_count"]):
        assert client.get(f"/novel-reads/{manifest['read_id']}/{index}").status_code == 200
    reconciled = client.post(base + "/reconciliation", json={
        "expected_revision": status["revision"], "confirmed_against_raw": True,
        "unresolved_conflicts": [],
    })
    assert reconciled.status_code == 200, reconciled.text
    finalized = client.post(base + "/finalize")
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["draft_id"] == did
    blocks = novel_drafts._read(did)["sections"]["intake"]["blocks"]
    assert {row["block_id"]: row["raw_text"] for row in blocks} == originals
    assert all(row["fact_ids"] == [] and row["reviewed_against_raw"] for row in blocks)
