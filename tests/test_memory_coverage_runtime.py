"""Only actual spoken commitments enter the speaker's personal memory."""
import json

import pytest

from app import operation_service, storage
from app.memory_coverage_runtime import durable_speech_cues, preserve_durable_dialogue
from test_generator_state_documents import create, prepare, story


@pytest.mark.parametrize("line", [
    "**Рен** *Обещаю вернуть ключи.*",
    "> **Рен** — Обещаю вернуть ключи.",
    "**Рен** (сообщение) — *Обещаю вернуть ключи.*",
])
def test_actual_spoken_promise_is_detected(line):
    assert durable_speech_cues(line) == [{"speaker": "Рен", "quote": "Обещаю вернуть ключи."}]


@pytest.mark.parametrize("line", [
    "**Рен** — *(вспомнил её «обещаю»)* Я тебе не верю.",
    "**Рен** *(вспомнил её «обещаю»)* Я тебе не верю.",
    "**Рен** — *Я тебе не верю.* *(вспомнил её «обещаю»)*",
])
def test_remembered_promise_in_stage_direction_is_not_spoken(line):
    assert durable_speech_cues(line) == []
    payload = {"scene_output": line, "extracted": {}}
    preserve_durable_dialogue("unused-session", payload)
    assert payload["extracted"] == {}


def test_choices_and_plain_emotions_do_not_create_commitments():
    assert durable_speech_cues("**Рен** — Свали, блять.") == []
    assert durable_speech_cues("**Рен** — Ты кто?\nЧто я могу сделать:\n"
                              "1. Уйти.\nЧто я могу сказать:\n**Рен** — Обещаю вернуться.") == []


def test_commit_saves_only_speakers_words_and_replay_does_not_duplicate_them(story):
    sid, root = create(story)
    manifest, _ = prepare(sid)
    payload = {"packet_id": manifest["packet_id"], "user_input": "Продолжаю разговор",
               "scene_output": "**Рен** — *(пожал плечами)* Обещаю вернуть ключи.",
               "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True,
                             "knowledge_reviewed": True}}
    operation_service.commit_turn_request(sid, payload)
    operation_service.commit_turn_request(sid, payload)
    memory = storage._read_json(root / "memory.json", {})
    own = memory["characters"]["ren"]["experiences"]
    assert sum(row.get("summary") == "Собственные слова: Обещаю вернуть ключи." for row in own) == 1
    for cid in ("pov", "away"):
        assert "Обещаю вернуть ключи" not in json.dumps(memory["characters"][cid], ensure_ascii=False)
    assert len(storage._read_turns(root)) == 1


@pytest.mark.parametrize("speaker", ["Незнакомец", "Общее имя"])
def test_unknown_or_ambiguous_speaker_does_not_receive_an_invented_memory(story, speaker):
    if speaker == "Общее имя":
        for card in story["characters"]:
            card["aliases"] = ["Общее имя"]
    sid, _ = create(story)
    payload = {"scene_output": f"**{speaker}** — Обещаю вернуть ключи.", "extracted": {}}
    preserve_durable_dialogue(sid, payload)
    assert payload["extracted"].get("experiences_add") == []
