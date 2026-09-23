import json
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from app import storage
from app.operation_service import commit_turn_request, prepare_turn_request


ROOT = Path(__file__).resolve().parents[1]


def setup_temp_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "knowledge-review",
        "title": "Knowledge Review",
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True},
            {"character_id": "liam", "name": "Лиам"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {"location": "room", "present_characters": ["elena", "liam"]},
            "relationships": {"liam": {"доверие": 2}},
        },
    }


def read_packet(sid: str, manifest: dict) -> dict:
    parts = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if parts else 0
    for index in range(start, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def scene() -> str:
    return """🎭 Knowledge Review · весна
🕒 День 1 · 12:00 · 📍 room
🌦️ Погода: спокойно
⚙️ Сцена: проверка знаний
✦ Елена
🧥 Одежда, волосы: обычно
--------------------------------------------------------

Елена посмотрела на Лиама.

Что я могу сделать:
1. Остаться на месте.
2. Подойти ближе.
3. Отойти.

Что я могу сказать:
1. Ответить.
2. Задать вопрос.
3. Промолчать.

Что я могу подумать:
1. О разговоре.
2. О комнате.
3. О Лиаме.

Состояние: спокойно
Отношения:
Лиам - доверие 2

Ход 1 · цикл 1/15"""


def extracted(*, reviewed=None) -> dict:
    value = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
    }
    if reviewed is not None:
        value["knowledge_reviewed"] = reviewed
    return value


def test_packet_knowledge_review_applies_to_pov_and_npcs_and_quarantines_author_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(
            sid,
            "(посмотреть на Лиама)",
            "knowledge-review-1",
            knowledge_review_capable=True,
        )
        context = read_packet(sid, manifest)

        guards = context["scene_logic_guardrails"]
        assert guards["version"] == 5
        assert guards["knowledge_causality"]["applies_to"] == "real speech only"
        author_only = " ".join(guards["knowledge_causality"]["author_only_not_character_knowledge"]).casefold()
        assert "chronology" in author_only
        assert "scene_history" in author_only
        assert "character card" in author_only
        assert "foundation" in author_only
        review = guards["knowledge_review"]
        assert review["applies_to"] == "real speech only"
        assert review["older_memory_retrieval"] == "prepareCharacterBundleRead(character_id)"
        assert "перепиши" in review["rule"]
        assert "knowledge_reviewed=true" in review["rule"]


def test_capable_packet_rejects_commit_until_knowledge_review_is_confirmed_then_allows_same_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        manifest = prepare_turn_request(
            sid,
            "(посмотреть на Лиама)",
            "knowledge-review-2",
            knowledge_review_capable=True,
        )
        read_packet(sid, manifest)

        payload = {
            "packet_id": manifest["packet_id"],
            "user_input": "(посмотреть на Лиама)",
            "scene_output": scene(),
            "extracted": extracted(),
        }
        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, payload)

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "KNOWLEDGE_REVIEW_REQUIRED"
        assert exc.value.detail["packet_id"] == manifest["packet_id"]
        assert "каждую реальную реплику" in exc.value.detail["instruction"]
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 0
        assert storage._read_json(root / "turn_packet.json", {}).get("packet_id") == manifest["packet_id"]

        payload["extracted"] = extracted(reviewed=True)
        result = commit_turn_request(sid, payload)
        assert result["turn_number"] == 1


def test_legacy_packet_without_capability_is_not_blocked_by_new_gate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "(посмотреть на Лиама)", "knowledge-review-legacy")
        read_packet(sid, manifest)

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на Лиама)",
                "scene_output": scene(),
                "extracted": extracted(),
            },
        )
        assert result["turn_number"] == 1


def test_openapi_exposes_optional_knowledge_review_capability_and_commit_flag():
    schema = yaml.safe_load((ROOT / "openapi.yaml").read_text(encoding="utf-8"))
    prepare = schema["components"]["schemas"]["TurnPrepare"]
    assert "knowledge_review_capable" in prepare["properties"]

    extracted_schema = schema["components"]["schemas"]["TurnCommit"]["properties"]["extracted"]
    assert "knowledge_reviewed" in extracted_schema["properties"]
    assert "knowledge_reviewed" not in extracted_schema["required"]
