import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as client:
        yield client


def rpc(client, method, params):
    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call(client, name, arguments):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments})


def payload(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_handshake_catalog_and_existing_rest(client):
    init = rpc(client, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    })
    assert init["serverInfo"]["name"] == "Roman AI"
    tools = {tool["name"]: tool for tool in rpc(client, "tools/list", {})["tools"]}
    spec = yaml.safe_load((Path(__file__).parents[1] / "openapi.yaml").read_text())
    expected = {
        op["operationId"] for methods in spec["paths"].values()
        for op in methods.values() if isinstance(op, dict) and "operationId" in op
    }
    assert set(tools) == expected
    assert len(tools) == 30
    assert "getCharacterMemory" not in tools
    assert set(tools["commitTurn"]["inputSchema"]["required"]) == {"session_id", "body"}
    assert client.get("/health").json() == {"ok": True}
    assert "/sessions/{session_id}/turns" in client.get("/openapi.json").json()["paths"]


def test_draft_raw_and_retry_share_rest_storage(client, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    draft = payload(call(client, "createNovelDraft", {
        "body": {"novel_id": "mcp_test", "title": "Тест MCP", "version": 5},
    }))
    did = draft["draft_id"]
    arguments = {"draft_id": did, "body": {
        "block_id": "first", "stage": "novel", "chunk_index": 0,
        "raw_text": "Героиня сказала: «Не меняй мои слова».\nВторая строка.", "is_last": True,
    }}
    payload(call(client, "appendDraftIntakeChunk", arguments))
    saved = (tmp_path / "novel_drafts" / f"{did}.json").read_bytes()
    payload(call(client, "appendDraftIntakeChunk", arguments))
    assert (tmp_path / "novel_drafts" / f"{did}.json").read_bytes() == saved
    status = payload(call(client, "getNovelDraftStatus", {"draft_id": did}))
    assert status == client.get(f"/novel-drafts/{did}").json()
    assert arguments["body"]["raw_text"] in json.dumps(json.loads(saved), ensure_ascii=False).replace('\\n', '\n')


def test_backend_and_input_errors_are_not_success(client, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    missing = call(client, "getNovelDraftStatus", {"draft_id": "missing"})
    assert missing["isError"] is True
    assert "HTTP 404" in missing["content"][0]["text"]
    invalid = call(client, "createNovelDraft", {"body": {"version": 5}})
    assert invalid["isError"] is True
    assert not list(tmp_path.rglob("*.json"))


def test_reject_untrusted_host(client):
    response = client.post(
        "/mcp", headers={"Host": "untrusted.example", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 421
