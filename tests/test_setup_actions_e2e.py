"""Exercise the advertised Actions over HTTP, beginning before an анкета exists."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from app import character_chunk_read, novel_drafts, session_documents, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    storage.ensure_dirs()
    with TestClient(app) as value:
        yield value


def post(client, path, body=None):
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def new_draft(client, request_id=None):
    # Deliberately omit version: static and live schemas must agree on v5.
    body = {"novel_id": "setup-http", "title": "Аудит"}
    if request_id is not None:
        body["request_id"] = request_id
    draft = post(client, "/novel-drafts", body)
    assert draft["version"] == 5
    assert draft["simple_profile_mode"] is True
    assert draft["profile_schema"]["save_requires_expected_revision"] is True
    return draft


def complete_setup(client, request_id=None):
    draft = new_draft(client, request_id=request_id)
    did = draft["draft_id"]
    base = f"/novel-drafts/{did}"
    raw = ("  Елена знакома с Реном. Рен знает Елену.\n" * 400) + "КОНЕЦ\n"
    upload = {"block_id": "raw", "stage": "source", "chunk_index": 0,
              "full_raw": True, "raw_text": raw}
    saved = post(client, base + "/intake/chunks", upload)
    assert saved["complete"] is True
    assert saved["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert saved["char_count"] == len(raw)
    replay = post(client, base + "/intake/chunks", upload)
    assert replay["already_completed"] is True
    revision = saved["draft_revision"]

    # Use the actual Russian templates, including their notes, rather than a
    # hand-crafted English-only shortcut that conceals setup incompatibilities.
    novel = session_documents.template("анкета.json")
    novel.update({"название": "Аудит", "pov_id": "pov", "дата_начала": "21.10.2026"})
    cards = []
    for cid, name in (("pov", "Елена"), ("ren", "Рен"), ("away", "Мира")):
        card = session_documents.template("карточка_персонажа.json")
        card.update({"id": cid, "имя": name})
        cards.append(card)
    cards[1]["relationships"] = [{"target_character_id": "pov",
                                  "dimensions": [{"label": "доверие", "value": 25}]}]
    sections = {
        "novel": novel,
        "characters": cards,
        "knowledge": {"pov": ["Знакома с Реном."], "ren": ["Знаком с Еленой."],
                      "away": ["Знает свой личный секрет."]},
        "hidden_lore": {"entries": ["Тайна Мира неизвестна Елене."]},
    }
    for name, value in sections.items():
        draft = post(client, base + "/sections", {
            "section_name": name, "section_json": json.dumps(value, ensure_ascii=False),
            "expected_revision": revision,
        })
        revision = draft["revision"]
    assert "_памятка" not in json.dumps(novel_drafts._read(did)["sections"]["novel"], ensure_ascii=False)
    reviewed = post(client, base + "/intake/raw/mapping", {
        "fact_ids": [], "reviewed_against_raw": True, "expected_revision": revision,
    })
    revision = reviewed["revision"]
    premature = client.post(base + "/finalize")
    assert premature.status_code == 409
    assert premature.json()["detail"] == "INTAKE_FINAL_READ_REQUIRED"

    manifest = post(client, base + "/read")
    assert manifest["chunk_count"] > 1
    post_read = client.get(f"/novel-reads/{manifest['read_id']}/0")
    assert post_read.status_code == 200
    resumed = post(client, base + "/read")
    assert resumed["read_id"] == manifest["read_id"]
    assert resumed["next_chunk_index"] == 1
    for index in range(1, manifest["chunk_count"]):
        assert client.get(f"/novel-reads/{manifest['read_id']}/{index}").status_code == 200

    post(client, base + "/reconciliation", {
        "expected_revision": revision, "confirmed_against_raw": True, "unresolved_conflicts": [],
    })
    finalized = post(client, base + "/finalize")
    assert finalized["finalized_revision"] == revision
    assert finalized["awaiting_launch_start"] is True
    post(client, base + "/launch-state", {
        "expected_finalized_revision": finalized["finalized_revision"],
        "starting_state_json": json.dumps({
            "current": {"date": "21.10.2026", "time": "10:00", "location": "комната",
                        "present_characters": ["pov", "ren"]},
            "pov": {"character_id": "pov"},
        }, ensure_ascii=False),
    })
    created = post(client, base + "/session")
    sid = created["session_id"]
    assert post(client, base + "/session")["session_id"] == sid
    assert storage.load_session(sid)["source"]["source_intake"][0]["raw_text"] == raw
    return sid


def test_same_title_in_another_chat_creates_isolated_v5_playthrough(client):
    first_key = uuid4().hex
    first_sid = complete_setup(client, request_id=first_key)
    first = storage.load_session(first_sid)
    first_root = storage.SESSIONS_DIR / first_sid

    # Existing playthrough has advanced. Starting the same canon elsewhere
    # must neither adopt these events nor reset this save.
    first["meta"]["turn_number"] = 7
    first["state"]["current"].update(date="22.10.2026", location="старое прохождение")
    storage._write_json(first_root / "meta.json", first["meta"])
    storage._write_json(first_root / "state.json", first["state"])
    storage._write_json(first_root / "chronology.json", [{"turn": 7, "summary": "OLD_RUN_EVENT"}])
    storage._write_json(first_root / "memory.json", {"characters": {"pov": {"knowledge": ["OLD_RUN_KNOWLEDGE"]}}})
    (first_root / "turns.jsonl").write_text(json.dumps({
        "turn_number": 7, "user_input": "OLD_RUN_INPUT", "scene_output": "OLD_RUN_SCENE",
    }) + "\n", encoding="utf-8")
    before = {p.relative_to(first_root): p.read_bytes() for p in first_root.rglob("*") if p.is_file()}

    second_key = uuid4().hex
    second_sid = complete_setup(client, request_id=second_key)
    second = storage.load_session(second_sid)
    assert second_sid != first_sid
    assert second["meta"]["source_novel_id"] == first["meta"]["source_novel_id"]
    assert second["meta"]["source_draft_id"] != first["meta"]["source_draft_id"]
    assert second["meta"]["turn_number"] == 0
    assert second["state"]["current"]["date"] == "21.10.2026"
    assert second["state"]["current"]["location"] == "комната"
    assert second["chronology"] == []
    assert second["recent_turns"] == []
    assert "OLD_RUN_" not in json.dumps(second, ensure_ascii=False)

    # Lost replies remain retryable within each playthrough, even after play.
    for key, sid in ((first_key, first_sid), (second_key, second_sid)):
        draft = new_draft(client, request_id=key)
        replay = post(client, f"/novel-drafts/{draft['draft_id']}/session")
        assert replay["session_id"] == sid
        assert replay["idempotent_replay"] is True
    assert len(list(storage.SESSIONS_DIR.iterdir())) == 2
    assert before == {p.relative_to(first_root): p.read_bytes() for p in first_root.rglob("*") if p.is_file()}


def read_packet(client, sid, manifest):
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        value = client.get(f"/sessions/{sid}/turn-packet/{manifest['packet_id']}/{index}")
        assert value.status_code == 200
        parts.append(value.json()["content"])
    return json.loads("".join(parts))


def scene(turn):
    exchange = "**Рен** — Давай встретимся завтра.\n" if turn == 1 else ""
    delta = "0.5" if turn == 1 else "0"
    return ("🎭 Аудит · осень\n🕒 День 1 · среда, 21.10.2026, 10:00 ·\n"
            "📍 комната 🌦️ Погода: ясно\n⚙️ Сцена: встреча\n"
            "✦ Елена 🧥 Одежда, волосы: свитер ◈ Инвентарь: телефон\n\n"
            "--------------------------------------------------------\n"
            f"Елена и Рен продолжают разговор. Сохранённый эпизод {turn}.\n{exchange}\n"
            "Что я могу сделать:\n1. Подойти.\n2. Сесть.\n3. Выйти.\n\n"
            "Что я могу сказать:\n1. Да.\n2. Нет.\n3. Продолжай.\n\n"
            "Что я могу подумать:\n1. Интересно.\n2. Странно.\n3. Посмотрим.\n\n"
            f"Состояние: спокойствие\nОтношения:\nРен - доверие 25.5/{delta}\n\nХод {turn} · цикл {((turn-1)%15)+1}/15")


def mcp_rpc(client, method, params):
    response = client.post(
        "/mcp/", headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_mcp_exports_current_actions_and_runs_the_same_turn(client, monkeypatch):
    character_tools = {"prepareCharacterBundleRead", "getCharacterBundleChunk"}

    def call(name, **arguments):
        return mcp_rpc(client, "tools/call", {"name": name, "arguments": arguments})

    def result(name, **arguments):
        reply = call(name, **arguments)
        assert not reply.get("isError"), reply
        text_result = json.loads(reply["content"][0]["text"])
        if name in character_tools | {"prepareTurn", "getTurnPacketChunk"}:
            assert reply["structuredContent"] == text_result
        return text_result

    init = mcp_rpc(client, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "romanai-test", "version": "1"},
    })
    assert init["serverInfo"]["name"] == "Roman AI"
    assert client.post("/mcp/", headers={"Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202

    catalog = {tool["name"]: tool for tool in mcp_rpc(client, "tools/list", {})["tools"]}
    contract = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi.yaml").read_text(encoding="utf-8"))
    actions = {op["operationId"] for methods in contract["paths"].values() for op in methods.values()}
    assert set(catalog) == actions | {"show_scene_choices", "check_scene_choices"}
    assert catalog["listNovels"]["annotations"]["readOnlyHint"] is True
    assert catalog["getTurnPacketChunk"]["annotations"]["readOnlyHint"] is False
    assert catalog["commitTurn"]["annotations"]["readOnlyHint"] is False
    assert set(catalog["commitTurn"]["inputSchema"]["required"]) == {"session_id", "body"}
    assert "content" in catalog["prepareTurn"]["outputSchema"]["properties"]
    assert "content" in catalog["getTurnPacketChunk"]["outputSchema"]["required"]
    for name in character_tools:
        assert "content" in catalog[name]["outputSchema"]["required"]
    assert result("listNovels") == client.get("/novels").json()

    draft = result("createNovelDraft", body={"novel_id": "mcp-raw", "title": "RAW", "request_id": "mcp-draft"})
    raw = "  Дословный RAW.\r\nПоследняя строка.\n"
    uploaded = result("appendDraftIntakeChunk", draft_id=draft["draft_id"], body={
        "block_id": "raw", "stage": "source", "chunk_index": 0, "full_raw": True, "raw_text": raw,
    })
    assert uploaded["raw_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert result("listNovels", include_drafts=True, draft_title="RAW")["drafts"][0]["draft_id"] == draft["draft_id"]

    sid = complete_setup(client)
    with monkeypatch.context() as patch:
        patch.setattr(character_chunk_read, "CHARACTER_CHUNK_CHARS", 256)
        dossier = result("prepareCharacterBundleRead", session_id=sid, character_id="away")
        assert dossier["character_id"] == "away"
        assert dossier["first_chunk_included"] is True
        assert dossier["chunk_count"] > 1
        dossier_parts = [dossier["content"]]
        for index in range(1, dossier["chunk_count"]):
            chunk = result("getCharacterBundleChunk", session_id=sid, character_id="away",
                           read_id=dossier["read_id"], chunk_index=index)
            assert chunk["chunk_index"] == index
            dossier_parts.append(chunk["content"])
        assert chunk["next_chunk_index"] is None
        assert json.loads("".join(dossier_parts))["character_id"] == "away"
    prepared = result("prepareTurn", session_id=sid, body={"opening_scene": True, "user_input": "", "request_id": "mcp-opening"})
    assert prepared["chunk_count"] > 1
    packet_id = prepared["packet_id"]
    commit = {"packet_id": packet_id, "user_input": "", "scene_output": scene(1),
              "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True}}
    blocked = call("commitTurn", session_id=sid, body=commit)
    assert blocked["isError"] is True
    assert "TURN_PACKET_INCOMPLETE" in blocked["content"][0]["text"]
    parts = [prepared["content"]]
    total = prepared["chunk_count"]
    assert total >= 4
    for index in range(1, total, 3):
        batch = result("getTurnPacketChunk", session_id=sid, packet_id=packet_id,
                       chunk_index=index, chunk_limit=3)
        indices = list(range(index, min(index + 3, total)))
        assert batch["chunk_indices"] == indices
        assert batch["unread_chunk_indices"] == list(range(indices[-1] + 1, total))
        assert batch["all_chunks_read"] == (indices[-1] == total - 1)
        singles = [
            result("getTurnPacketChunk", session_id=sid, packet_id=packet_id, chunk_index=i)["content"]
            for i in indices
        ]
        assert batch["content"].encode() == "".join(singles).encode()
        parts.extend(singles)
    assert json.loads("".join(parts))["scene_builder"]
    reread = result("getTurnPacketChunk", session_id=sid, packet_id=packet_id, chunk_index=1)
    assert reread["content"] == parts[1]
    assert reread["all_chunks_read"] is True
    assert result("commitTurn", session_id=sid, body=commit)["turn_number"] == 1
    assert result("commitTurn", session_id=sid, body=commit)["idempotent_replay"] is True
    assert result("prepareTurn", session_id=sid, body={
        "opening_scene": True, "user_input": "", "request_id": "mcp-opening",
    })["already_committed_duplicate"] is True
    assert post(client, f"/sessions/{sid}/resume")["turn_number"] == 1
    assert call("show_scene_choices", session_id=sid, expected_turn=1)["structuredContent"]["turn_number"] == 1
    invalid = call("resumeSession", session_id="../outside")
    assert invalid["isError"] is True
    assert "HTTP 422" in invalid["content"][0]["text"]


def test_compact_header_survives_commit_resume_next_packet_and_choice_read(client):
    from app.scene_choices import saved_scene_choices
    from test_scene_choices import SCENE
    sid = complete_setup(client)
    base = f"/sessions/{sid}"
    prepared = post(client, base + "/turn-packet", {
        "opening_scene": True, "user_input": "", "request_id": "compact-opening"})
    read_packet(client, sid, prepared)
    output = SCENE.replace("Ход 2 · цикл 2/15", "Ход 1 · цикл 1/15")
    post(client, base + "/turns", {
        "packet_id": prepared["packet_id"], "user_input": "", "scene_output": output,
        "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True},
    })
    current = storage.load_session(sid)["state"]["current"]
    assert current["date"] == "22.10.2026"
    assert current["time"] == "14:20"
    assert current["location"] == "мастерская"
    assert current["game_day"] == 2
    assert saved_scene_choices(sid, 1)["groups"][0]["items"][0]["text"] == "Подойти."
    assert post(client, base + "/resume")["turn_number"] == 1
    prepared = post(client, base + "/turn-packet", {
        "user_input": "(Подойти.) Продолжай. (Интересно.)", "request_id": "compact-next"})
    packet = read_packet(client, sid, prepared)
    assert packet["scene_state"]["current"]["game_day"] == 2


def test_anketa_to_61_http_turns_retry_resume_manual_audit_and_rollback(client):
    sid = complete_setup(client)
    base = f"/sessions/{sid}"
    assert post(client, base + "/resume")["turn_number"] == 0
    bundle = post(client, base + "/characters/away/read")
    parts = [bundle["content"]]
    for index in range(1, bundle["chunk_count"]):
        chunk = client.get(f"{base}/characters/away/read/{bundle['read_id']}/{index}")
        assert chunk.status_code == 200
        parts.append(chunk.json()["content"])
    own_bundle = json.loads("".join(parts))
    assert own_bundle["character_id"] == "away"
    assert "личный секрет" in json.dumps(own_bundle["knowledge_journal"], ensure_ascii=False)
    assert own_bundle["knowledge_complete"] is True
    opening_request = {"opening_scene": True, "user_input": "", "request_id": "opening"}
    first_commit = None
    for turn in range(1, 62):
        request = opening_request if turn == 1 else {"user_input": f"(Продолжить {turn})", "request_id": f"turn-{turn}"}
        prepared = post(client, base + "/turn-packet", request)
        context = read_packet(client, sid, prepared)
        assert list(context)[:3] == ["scene_builder", "runtime_rules", "read_order"]
        assert set(context["character_memory"]) == {"pov", "ren"}
        assert "writer_cue" not in json.dumps(context.get("waiting_momentum", {}))
        assert "source_intake" not in context
        extracted = {"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True}
        if turn == 1:
            extracted.update({
                "knowledge_journal_add": [{"character_id": "pov", "text": "Рен назначил встречу на завтра."}],
                "chronology": [{"event": "Рен назначил встречу на завтра.", "participants": ["pov", "ren"]}],
                "relationship_updates": [{"character_id": "ren", "dimensions": [{"label": "доверие", "delta": 0.5}]}],
            })
        payload = {"packet_id": prepared["packet_id"], "user_input": request["user_input"],
                   "scene_output": scene(turn), "extracted": extracted}
        result = post(client, base + "/turns", payload)
        assert result["turn_number"] == turn
        assert result.get("handoff_required") is not True
        if turn == 1:
            first_commit = deepcopy(payload)
            assert post(client, base + "/turns", payload)["idempotent_replay"] is True
            # A lost response must not make the opening command unrepeatable.
            assert post(client, base + "/turn-packet", opening_request)["already_committed_duplicate"] is True
        if turn in {15, 45, 60}:
            resumed = post(client, base + "/resume")
            assert resumed["turn_number"] == turn
            assert resumed["audit_available"] is True
            assert "audit_manifest" not in resumed

    resumed = post(client, base + "/resume")
    assert resumed["last_committed_turn"]["scene_output"] == scene(61)
    root = storage.SESSIONS_DIR / sid
    assert len(storage._read_turns(root)) == 61
    assert storage._read_json(root / "relationships.json", {})["npc_to_pov"]["ren"]["dimensions"]["доверие"]["value"] == 25.5
    assert "встречу на завтра" in json.dumps(session_documents.read(root / "память/pov.json"), ensure_ascii=False)
    assert "встречу на завтра" not in json.dumps(session_documents.read(root / "память/away.json"), ensure_ascii=False)
    assert post(client, base + "/turns", first_commit)["idempotent_replay"] is True
    assert len(storage._read_turns(root)) == 61

    rollback = {"expected_turn_number": 61, "expected_turn_id": resumed["current_turn_id"], "confirm": True}
    result = post(client, base + "/rollback-last-turn", rollback)
    assert result["turn_number"] == 60
    assert post(client, base + "/rollback-last-turn", rollback)["idempotent_replay"] is True
    # Backlogged manual audit remains available without blocking the next turn.
    audit = post(client, base + "/resume?prepare_audit=true")["audit_manifest"]
    assert audit["audit_range"] == [1, 15]
    for index in range(audit["chunk_count"]):
        assert client.get(f"{base}/turn-packet/{audit['audit_id']}/{index}").status_code == 200
    repairs = {"scene_compactions": [{
        "start_turn": 1, "end_turn": 15, "participants": ["pov", "ren"],
        "location": "комната", "status": "closed",
        "summary": "Елена и Рен провели встречу в комнате. Рен назначил встречу на следующий день; Елена услышала договорённость, доверие Рена изменилось с 25 до 25.5. Разговор продолжился без смены места.",
    }]}
    audited = post(client, base + "/turns", {"audit_id": audit["audit_id"], "start_turn": 1,
                                            "end_turn": 15, "repairs": repairs, "notes": []})
    assert audited["audited_through"] == 15
    assert post(client, base + "/turn-packet", {"user_input": "(Дальше)", "request_id": "after-audit"})["prepared_for_turn"] == 61


@pytest.mark.parametrize("section,value,detail", [
    ("novel", [], "novel must be a JSON object"),
    ("characters", {"pov": {"имя": "Елена"}}, "characters must be a JSON array"),
    ("characters", [{"id": "pov", "имя": "Елена"}, {"id": "pov", "имя": "Мира"}], "DRAFT_CHARACTER_ID_DUPLICATE"),
    ("characters", [{"id": "../pov", "имя": "Елена"}], "SESSION_DOCUMENT_CHARACTER_ID_INVALID"),
    ("characters", [{}], "DRAFT_CHARACTER_ID_AND_NAME_REQUIRED"),
    ("knowledge", [], "knowledge must be a JSON object"),
])
def test_invalid_setup_shapes_do_not_silently_erase_or_drop_source(client, section, value, detail):
    draft = new_draft(client)
    response = client.post(f"/novel-drafts/{draft['draft_id']}/sections", json={
        "section_name": section, "section_json": json.dumps(value), "expected_revision": draft["revision"],
    })
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    stored = novel_drafts._read(draft["draft_id"])
    assert stored["revision"] == draft["revision"]
    assert stored["sections"] == {}


def test_live_and_static_draft_defaults_agree_and_legacy_version_is_explicit(client):
    import yaml
    from pathlib import Path
    static = yaml.safe_load(Path("openapi.yaml").read_text())
    live = client.get("/openapi.json").json()
    for schema in (static, live):
        assert schema["components"]["schemas"]["NovelDraftCreate"]["properties"]["version"]["default"] == 5
    assert post(client, "/novel-drafts", {"novel_id": "old", "title": "Old", "version": 1})["version"] == 1


def test_missing_revision_is_explicit_before_first_profile_write(client):
    draft = new_draft(client)
    result = client.post(f"/novel-drafts/{draft['draft_id']}/sections", json={
        "section_name": "novel", "section_json": '{"pov_id":"pov"}',
    })
    assert result.status_code == 409
    assert result.json()["detail"] == "DRAFT_SECTION_REVISION_REQUIRED"


@pytest.mark.parametrize("reference,flags,blocker", [
    ("pov", [True, True], "DRAFT_POV_AMBIGUOUS"),
    ("pov", [False, True], "DRAFT_POV_CONFLICT"),
    ("missing", [True, False], "DRAFT_POV_UNKNOWN"),
])
def test_conflicting_pov_is_reported_before_final_read(client, reference, flags, blocker):
    draft = new_draft(client)
    base = f"/novel-drafts/{draft['draft_id']}"
    saved = post(client, base + "/intake/chunks", {
        "block_id": "raw", "stage": "source", "chunk_index": 0,
        "raw_text": "Елена и Мира встречаются в комнате.", "full_raw": True,
    })
    saved = post(client, base + "/sections", {
        "section_name": "novel", "section_json": json.dumps({"pov_id": reference}),
        "expected_revision": saved["draft_revision"],
    })
    cards = [{"id": "pov", "имя": "Елена", "is_pov": flags[0]},
             {"id": "other", "имя": "Мира", "is_pov": flags[1]}]
    saved = post(client, base + "/sections", {
        "section_name": "characters", "section_json": json.dumps(cards),
        "expected_revision": saved["revision"],
    })
    assert saved["finalize_blocker"] == blocker
    response = client.post(base + "/finalize")
    assert response.status_code == 409
    assert response.json()["detail"] == blocker


def test_full_size_turn_packet_batch_is_lossless_over_mcp(client):
    sid = "batch-test"
    root = storage.SESSIONS_DIR / sid
    root.mkdir()
    chunks = [str(index).rjust(2, "0") + "Х" * 15998 for index in range(18)]
    storage._write_json(root / "turn_packet.json", {
        "packet_id": "batch-packet", "chunks": chunks, "chunk_count": len(chunks),
        "read_chunks": [0],
    })
    responses = []
    for index in range(1, len(chunks), 3):
        result = mcp_rpc(client, "tools/call", {
            "name": "getTurnPacketChunk",
            "arguments": {"session_id": sid, "packet_id": "batch-packet",
                          "chunk_index": index, "chunk_limit": 3},
        })
        assert result.get("isError") is not True, result
        batch = result["structuredContent"]
        assert json.loads(result["content"][0]["text"])["content"] == batch["content"]
        responses.append(batch["content"])
    assert len(responses) == 6
    assert (chunks[0] + "".join(responses)).encode() == "".join(chunks).encode()
    assert batch["unread_chunk_indices"] == []
    assert batch["all_chunks_read"] is True


def test_mcp_read_schema_and_traces_distinguish_repeat_reads(client, caplog):
    caplog.set_level("INFO", logger="uvicorn.error")
    catalog = {row["name"]: row for row in mcp_rpc(client, "tools/list", {})["tools"]}
    props = catalog["prepareTurn"]["outputSchema"]["properties"]
    assert {"unread_chunk_indices", "all_chunks_read", "reused_pending_packet", "read_chunks"} <= props.keys()
    sid = "trace-test"
    root = storage.SESSIONS_DIR / sid
    root.mkdir()
    private = "PRIVATE_RAW_AND_SCENE_MUST_NOT_APPEAR_IN_TRACE"
    storage._write_json(root / "turn_packet.json", {
        "packet_id": "trace-packet", "chunks": [private] * 4,
        "chunk_count": 4, "read_chunks": [0],
    })
    arguments = {"session_id": sid, "packet_id": "trace-packet", "chunk_index": 1, "chunk_limit": 3}
    first = mcp_rpc(client, "tools/call", {"name": "getTurnPacketChunk", "arguments": arguments})
    second = mcp_rpc(client, "tools/call", {"name": "getTurnPacketChunk", "arguments": arguments})
    assert first["structuredContent"]["previously_read_chunk_indices"] == []
    assert second["structuredContent"]["previously_read_chunk_indices"] == [1, 2, 3]
    assert second["structuredContent"]["all_chunks_read"] is True
    assert second["structuredContent"]["unread_chunk_indices"] == []
    bad = mcp_rpc(client, "tools/call", {
        "name": "getTurnPacketChunk", "arguments": {**arguments, "packet_id": "stale"},
    })
    assert bad["isError"] is True
    rows = [json.loads(r.getMessage().split("romanai_tool ", 1)[1])
            for r in caplog.records if r.getMessage().startswith("romanai_tool ")]
    assert [r["event"] for r in rows] == ["start", "end", "start", "end", "start", "error"]
    assert rows[2]["call_id"] == rows[3]["call_id"] != rows[0]["call_id"]
    assert rows[3]["session_id"] == sid
    assert rows[3]["packet_id"] == "trace-packet"
    assert rows[3]["previously_read_chunk_indices"] == [1, 2, 3]
    assert rows[-1]["status"] == 403
    assert private not in json.dumps(rows)


def test_mcp_commit_trace_identifies_idempotent_replay_without_scene_text(client, monkeypatch, caplog):
    import app.main as main
    caplog.set_level("INFO", logger="uvicorn.error")
    calls = []
    def commit(sid, payload):
        replay = bool(calls)
        calls.append(payload)
        return {"turn_number": 7, "already_committed": replay, "idempotent_replay": replay}
    monkeypatch.setattr(main, "commit_turn_request", commit)
    args = {"session_id": "trace-game", "body": {
        "packet_id": "same-packet", "user_input": "PRIVATE_USER_INPUT",
        "scene_output": "PRIVATE_SCENE", "extracted": {},
    }}
    for _ in range(2):
        result = mcp_rpc(client, "tools/call", {"name": "commitTurn", "arguments": args})
        assert not result.get("isError"), result
    rows = [json.loads(r.getMessage().split("romanai_tool ", 1)[1])
            for r in caplog.records if r.getMessage().startswith("romanai_tool ")]
    ends = [r for r in rows if r["event"] == "end"]
    assert [r["idempotent_replay"] for r in ends] == [False, True]
    assert all(r["packet_id"] == "same-packet" and r["turn_number"] == 7 for r in ends)
    assert "PRIVATE_" not in json.dumps(rows)
