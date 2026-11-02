"""Repairs must preserve continuity without introducing more gameplay gates."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from app import character_chunk_read, novel_drafts, npc_intent, operation_service, storage
from app.private_knowledge_runtime import _speaker_units
from app.scene_choices import parse_choices
from app.scene_speech import iter_scene_speech
from app.session_recovery import _speaker_present_ids
from test_generator_state_documents import create, prepare, story
from test_simple_profile_flow import _build_simple_draft


@pytest.mark.parametrize("channel", ["сообщение", "звонок", "по телефону", "по рации"])
def test_builder_remote_dialogue_keeps_channel_without_physical_teleport(channel):
    cards = [{"character_id": "ren", "name": "Рен"}]
    line = f"**Рен** ({channel}) — *Обещаю вернуть ключи.*"
    parsed = list(iter_scene_speech(line))
    assert parsed == [{"speaker": "Рен", "channel": channel,
                       "text": "Обещаю вернуть ключи.", "position": 0}]
    assert _speaker_units(line, cards)[0]["character_id"] == "ren"
    assert channel in _speaker_units(line, cards)[0]["speaker_label"]
    assert _speaker_present_ids(line, cards=cards) == []


def test_saved_choice_headings_with_explanations_remain_readable():
    scene = "\n\n".join(f"Что я могу {verb} (в характере POV):\n1. Раз.\n2. Два.\n3. Три."
                          for verb in ("сделать", "сказать", "подумать"))
    assert len(parse_choices(scene)) == 3
    # A hypothetical quoted line in choices must not become a real speech event.
    assert list(iter_scene_speech(scene + "\n**Рен** — Обещаю всё исправить.")) == []


def test_offscreen_bundle_keeps_full_own_experience_and_plans(story):
    sid, root = create(story)
    state = storage._read_json(root / "state.json", {})
    state = npc_intent.apply_updates(state, [{"character_id": "away", "intent_id": "return_keys",
        "summary": "Вернуть ключи", "planned_action": "Привезти запасные ключи после работы",
        "priority": "high"}, *[{"character_id": "away", "intent_id": f"plan_{i}",
                                "summary": f"Незавершённый план {i}"} for i in range(7)]], current_turn=0)
    storage._write_json(root / "state.json", state)
    memory = storage._read_json(root / "memory.json", {})
    storage._memory_bucket(memory, "away")["experiences"] = [
        {"event_id": "own_experience", "summary": "Она оставила ключи у сестры.", "turn": 0}]
    storage._write_json(root / "memory.json", memory)
    manifest = character_chunk_read.prepare_character_bundle_read(sid, "away")
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(character_chunk_read.get_character_bundle_chunk(
            sid, "away", manifest["read_id"], index)["content"])
    bundle = json.loads("".join(parts))
    assert bundle["experiences"][0]["summary"] == "Она оставила ключи у сестры."
    assert bundle["active_intents"][0]["planned_action"] == "Привезти запасные ключи после работы"
    assert len(bundle["active_intents"]) == 8


def test_all_open_threads_are_available_without_new_read_actions(story):
    sid, root = create(story)
    state = storage._read_json(root / "state.json", {})
    state["threads"] = {f"t{i}": {"thread_id": f"t{i}", "summary": f"Открытая линия {i}",
                                  "status": "active", "priority": i} for i in range(14)}
    storage._write_json(root / "state.json", state)
    _, packet = prepare(sid)
    assert set(packet["active_threads"]) == set(state["threads"])
    assert "narrative_guardrails" not in packet


def test_promise_survives_later_turns_without_becoming_others_knowledge(story):
    sid, root = create(story)
    for turn in range(1, 18):
        text = f"(Осмотреть зал, момент {turn})"
        manifest, packet = prepare(sid, text, request_id=f"promise-{turn}")
        if turn == 17:
            assert "Обещаю вернуть запасные ключи завтра" in json.dumps(packet["character_memory"]["ren"], ensure_ascii=False)
        payload = {"packet_id": manifest["packet_id"], "user_input": text,
            "scene_output": "**Рен** — Обещаю вернуть запасные ключи завтра." if turn == 1 else "Елена смотрела в окно.",
            "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True,
                          "knowledge_reviewed": True,
                          "chronology": [{"event": "Елена осмотрела зал."}] if turn == 1 else []}}
        operation_service.commit_turn_request(sid, payload)
        if turn == 1:
            operation_service.commit_turn_request(sid, payload)  # lost-response replay
    memory = storage._read_json(root / "memory.json", {})
    records = memory["characters"]["ren"]["experiences"]
    assert sum("Обещаю вернуть запасные ключи завтра" in str(row) for row in records) == 1
    assert "ключи" not in json.dumps(memory["characters"]["away"], ensure_ascii=False)
    assert len(storage._read_turns(root)) == 17


def test_finalization_serializes_with_an_accepted_author_edit(story, monkeypatch):
    draft_id = _build_simple_draft()
    before = novel_drafts._read(draft_id)
    finalizing, release, edit_started, edit_done = Event(), Event(), Event(), Event()
    original_write = novel_drafts._write

    def paused_write(path, data):
        if data.get("finalized"):
            finalizing.set()
            assert release.wait(5)
        return original_write(path, data)

    monkeypatch.setattr(novel_drafts, "_write", paused_write)

    def edit():
        edit_started.set()
        novel = {**before["sections"]["novel"], "notes": "Важная новая авторская правка."}
        result = novel_drafts.save_section(draft_id, "novel", json.dumps(novel, ensure_ascii=False),
                                          expected_revision=before["revision"])
        edit_done.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        finalized = pool.submit(novel_drafts.finalize_draft, draft_id)
        assert finalizing.wait(5)
        edited = pool.submit(edit)
        try:
            assert edit_started.wait(5)
            assert not edit_done.wait(0.1)
        finally:
            release.set()
        assert finalized.result(5)["ok"]
        edited.result(5)
    after = novel_drafts._read(draft_id)
    assert after["revision"] == before["revision"] + 1
    assert after["sections"]["novel"]["notes"] == "Важная новая авторская правка."
    assert not after["finalized"]
