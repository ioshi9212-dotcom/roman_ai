"""Regression coverage for one write path, concurrent retries and hidden facts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import npc_intent, operation_service, session_runtime, storage
from app.main import app
from test_generator_state_documents import create, prepare, story


SECRET = "ЖАСМИНОВЫЙ_ЛЁД_731"
FACT = "Пароль архива — " + SECRET


def commit_payload(manifest, **updates):
    return {"packet_id": manifest["packet_id"], "user_input": "Продолжаю разговор",
            "scene_output": "**Рен** — Я тебя слышу.",
            "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True,
                          "knowledge_reviewed": True, **updates}}


@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("changed", [False, True])
def test_concurrent_http_retry_returns_receipt_or_conflict_without_missing_session(story, monkeypatch, version, changed):
    story["version"] = version
    sid, root = create(story)
    manifest, _ = prepare(sid)
    first = commit_payload(manifest, relationship_updates=[
        {"character_id": "ren", "dimensions": [{"label": "интерес", "delta": 0.25}]}])
    second = deepcopy(first)
    if changed:
        second["scene_output"] = "**Рен** — Другая сцена."
        second["extracted"]["relationship_updates"][0]["dimensions"][0]["delta"] = 0.5

    entered, release, second_started, second_finished = Event(), Event(), Event(), Event()
    calls = []
    original = session_runtime.commit_turn

    def paused_commit(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(session_runtime, "commit_turn", paused_commit)
    client = TestClient(app)

    def retry():
        second_started.set()
        try:
            return client.post(f"/sessions/{sid}/turns", json=second)
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(client.post, f"/sessions/{sid}/turns", json=first)
        try:
            assert entered.wait(5)
            b = pool.submit(retry)
            assert second_started.wait(5)
            # Let an unprotected second request race while the first is paused.
            second_finished.wait(0.15)
        finally:
            release.set()
        first_result, second_result = a.result(5), b.result(5)
    assert first_result.status_code == 200
    assert second_result.status_code == (409 if changed else 200)
    assert len(calls) == 1
    assert len(storage._read_turns(root)) == 1
    relation = storage._read_json(root / "relationships.json", {})
    assert relation["npc_to_pov"]["ren"]["dimensions"]["интерес"]["value"] == 12.25
    assert client.post(f"/sessions/{sid}/resume").status_code == 200


@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("raw_patch", [False, True])
def test_intent_knowledge_cannot_bypass_update_path(story, version, raw_patch):
    story["version"] = version
    sid, root = create(story)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    storage._memory_bucket(memory, "pov")["knowledge_journal"].append({
        "turn": 0, "text": "POV узнала от другого человека: выступление будет у стойки после семи."})
    storage._write_json(root / "memory.json", memory)
    manifest, _ = prepare(sid)
    intent = {"character_id": "ren", "intent_id": "show", "summary": "Прийти к стойке после семи",
              "planned_action": "Слушать выступление у стойки после семи"}
    updates = {"state_patch": {"npc_intents": {"ren": [intent]}}} if raw_patch else {"npc_intent_updates": [intent]}
    with pytest.raises(HTTPException) as error:
        operation_service.commit_turn_request(sid, commit_payload(manifest, **updates))
    assert error.value.status_code == 409
    assert error.value.detail["code"] == ("NPC_INTENTS_UPDATE_PATH_REQUIRED" if raw_patch else "NPC_INTENT_PERSONAL_KNOWLEDGE_LEAK")
    assert not npc_intent.active_intents_for(storage._read_json(root / "state.json", {}), ["ren"], current_turn=0)
    assert not storage._read_turns(root)
    corrected = {**intent, "summary": "Уточнить время и место выступления",
                 "planned_action": "Спросить POV про выступление"}
    operation_service.commit_turn_request(sid, commit_payload(manifest, npc_intent_updates=[corrected]))
    assert npc_intent.active_intents_for(storage._read_json(root / "state.json", {}), ["ren"], current_turn=1)


@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("evidence", ["none", "choices", "old_choices", "scene", "own_memory", "own_experience", "self_card", "absent", "owner_alias", "owner_field"])
def test_hidden_fact_needs_own_history_or_actual_scene_disclosure(story, version, evidence):
    story["version"] = version
    story["hidden_lore"] = {"entries": [FACT + ". Рен этого не знает."]}
    if evidence == "self_card":
        story["characters"][1]["notes"] = FACT
    sid, root = create(story)
    if evidence in {"own_memory", "own_experience"}:
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        field = "knowledge_journal" if evidence == "own_memory" else "experiences"
        storage._memory_bucket(memory, "ren")[field].append({
            "turn": 0, "text": FACT, "summary": FACT, "event_id": "known_fact"})
        storage._write_json(root / "memory.json", memory)
    manifest, _ = prepare(sid)
    owner = "away" if evidence == "absent" else "ren"
    data = commit_payload(manifest, knowledge_journal_add=[{"character_id": owner, "text": FACT}])
    if evidence == "owner_alias":
        data["extracted"]["knowledge_journal_add"][0]["character_id"] = "Рен"
    elif evidence == "owner_field":
        data["extracted"]["knowledge_journal_add"][0] = {"owner_character_id": owner, "text": FACT}
    if evidence in {"scene", "absent"}:
        data["scene_output"] = "*Рен развернул записку: «" + FACT + "».*"
    elif evidence in {"choices", "old_choices"}:
        heading = "Что я могу сделать (в характере POV):" if evidence == "old_choices" else "Что я могу сделать:"
        data["scene_output"] += "\n" + heading + "\n1. Узнать: " + FACT
    allowed = evidence in {"scene", "own_memory", "own_experience", "self_card"}
    if allowed:
        assert operation_service.commit_turn_request(sid, data)["turn_number"] == 1
        assert SECRET in str(storage._read_json(root / "memory.json", {}))
    else:
        with pytest.raises(HTTPException) as error:
            operation_service.commit_turn_request(sid, data)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "DIRECTOR_ONLY_JOURNAL_LEAK"
        assert not storage._read_turns(root)
        assert SECRET not in str(storage._read_json(root / "memory.json", {}))
        # Correct only the unsupported update, keep this packet and scene.
        data["extracted"]["knowledge_journal_add"] = []
        assert operation_service.commit_turn_request(sid, data)["turn_number"] == 1



@pytest.mark.parametrize("version", [1, 5])
@pytest.mark.parametrize("sender", ["away", "pov"])
@pytest.mark.parametrize("owner_field", ["character_id", "owner_character_id"])
def test_current_remote_disclosure_is_not_public_knowledge(story, version, sender, owner_field):
    story["version"] = version
    story["hidden_lore"] = {"entries": [FACT]}
    story["characters"][2]["notes"] = FACT
    sid, root = create(story)
    manifest, _ = prepare(sid)
    name = "Николь" if sender == "away" else "Елена"
    recipient = "pov" if sender == "away" else "away"
    data = commit_payload(manifest,
        state_patch={"current": {"remote_characters": ["away"], "remote_channels": {"away": "сообщение"}}},
        knowledge_journal_add=[{owner_field: "ren", "text": FACT}])
    data["scene_output"] = f"**{name}** (сообщение) — *{FACT}.*"
    with pytest.raises(HTTPException) as error:
        operation_service.commit_turn_request(sid, data)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PRIVATE_COMMUNICATION_JOURNAL_LEAK"
    assert not storage._read_turns(root)
    data["extracted"]["knowledge_journal_add"] = [{owner_field: recipient, "text": FACT}]
    assert operation_service.commit_turn_request(sid, data)["turn_number"] == 1
    memory = storage._read_json(root / "memory.json", {})["characters"]
    assert SECRET not in str(memory["ren"])
    assert SECRET in str(memory[recipient])
