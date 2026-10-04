import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import relationship_file_runtime, session_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "relationship-antifreeze",
        "title": "Relationship Antifreeze",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "relationships": [
                    {
                        "target_character_id": "rina",
                        "relationship_type": "давно знакомы",
                        "current_dynamic": "Доверяет, но всё ещё ждёт, что Рината снова уйдёт от разговора.",
                        "dimensions": [
                            {"label": "доверие", "value": 40},
                            {"label": "близость", "value": 25},
                        ],
                    }
                ],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "05.10.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def read_all_pending(session_id: str, manifest):
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def read_context(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "(посмотреть на Эдриана)")
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def payload(manifest, user_input: str, *, review=None, updates=None):
    return {
        "packet_id": manifest["packet_id"],
        "user_input": user_input,
        "scene_output": "Эдриан остаётся рядом и реагирует на Ринату.",
        "extracted": {
            "scene_builder_reviewed": True,
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "chronology": [],
            "knowledge_journal_add": [],
            "npc_intent_updates": [],
            "npc_relationship_updates": [],
            "story_thread_updates": [],
            "presence_updates": [],
            "relationship_review": review or [],
            "relationship_updates": updates or [],
            "state_patch": {},
            "character_upserts": [],
        },
    }


def test_api_turn_requires_one_relationship_review_for_participating_npc():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "(посмотреть на Эдриана)", request_id="review-required")
        read_all_pending(sid, manifest)

        assert manifest["relationship_review_required"] is True
        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, payload(manifest, "(посмотреть на Эдриана)"))
        assert exc.value.detail["code"] == "RELATIONSHIP_REVIEW_REQUIRED"
        assert exc.value.detail["character_ids"] == ["adrian"]


def test_relationship_review_cannot_claim_unchanged_while_sending_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "Спасибо.", request_id="review-contradiction")
        read_all_pending(sid, manifest)

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(
                sid,
                payload(
                    manifest,
                    "Спасибо.",
                    review=[{
                        "character_id": "adrian",
                        "changed": False,
                        "reason": "Проверено: отношение не изменилось.",
                    }],
                    updates=[{
                        "character_id": "adrian",
                        "reason": "Благодарность немного усилила доверие.",
                        "dimensions": [{"label": "доверие", "delta": 1}],
                    }],
                ),
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_REVIEW_UNCHANGED_WITH_UPDATE"


def test_relationship_review_changed_true_requires_real_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "Я пришла, как обещала.", request_id="review-needs-update")
        read_all_pending(sid, manifest)

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(
                sid,
                payload(
                    manifest,
                    "Я пришла, как обещала.",
                    review=[{
                        "character_id": "adrian",
                        "changed": True,
                        "reason": "Выполненное обещание изменило его отношение.",
                    }],
                ),
            )
        assert exc.value.detail["code"] == "RELATIONSHIP_REVIEW_CHANGED_WITHOUT_UPDATE"


def test_dynamic_only_shift_persists_without_forcing_numeric_jump_and_is_visible_next_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        before = storage._read_json(root / "relationships.json", {})
        before_values = {
            label: item["value"]
            for label, item in before["npc_to_pov"]["adrian"]["dimensions"].items()
        }

        manifest = prepare_turn_request(sid, "Я пришла, как обещала.", request_id="dynamic-only")
        read_all_pending(sid, manifest)
        result = commit_turn_request(
            sid,
            payload(
                manifest,
                "Я пришла, как обещала.",
                review=[{
                    "character_id": "adrian",
                    "changed": True,
                    "reason": "Выполненное обещание немного изменило текущую динамику.",
                }],
                updates=[{
                    "character_id": "adrian",
                    "reason": "Выполненное обещание уменьшило ожидание очередного отказа.",
                    "dynamic": "После выполненного обещания меньше ждёт от Ринаты очередного отказа.",
                }],
            ),
        )
        assert result["turn_number"] == 1

        after = storage._read_json(root / "relationships.json", {})
        after_relation = after["npc_to_pov"]["adrian"]
        after_values = {
            label: item["value"]
            for label, item in after_relation["dimensions"].items()
        }
        assert after_values == before_values
        assert "меньше ждёт" in after_relation["dynamic"]

        turns = storage._read_turns(root)
        assert "relationship_review" not in turns[-1].get("extracted", {})

        context = read_context(sid)
        row = next(
            item for item in context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        assert "меньше ждёт" in row["dynamic"]


def test_starting_current_dynamic_survives_without_numeric_dimensions():
    cards = [
        {"character_id": "rina", "name": "Рината", "is_pov": True},
        {
            "character_id": "tessa",
            "name": "Тэсса",
            "relationships": [{
                "target_character_id": "rina",
                "relationship_type": "настороженное знакомство",
                "current_dynamic": "Пока присматривается к Ринате и не решила, доверяет ли ей.",
                "dimensions": [],
            }],
        },
    ]
    store = relationship_file_runtime.build_initial_store(
        cards,
        {"pov": {"character_id": "rina"}},
        "rina",
    )
    assert store["npc_to_pov"]["tessa"]["dimensions"] == {}
    assert "присматривается" in store["npc_to_pov"]["tessa"]["dynamic"]
