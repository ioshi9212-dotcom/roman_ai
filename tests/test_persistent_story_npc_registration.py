import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import cast_registry_runtime, fast_audit_runtime, relationship_file_runtime, scene_presence_runtime, storage, turn_pipeline


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def make_session() -> str:
    novel = {
        "novel_id": "persistent-npc-gate",
        "title": "Persistent NPC Gate",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "Кайр", "is_pov": True},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "05.10.2026",
                "time": "20:00",
                "location": "bar",
                "present_characters": ["pov"],
            },
        },
    }
    return storage.create_session(novel)["session_id"]


def put_unknown_ada_in_persistent_presence(sid: str):
    root = storage.SESSIONS_DIR / sid
    state = storage._read_json(root / "state.json", {})
    state["current"]["present_characters"] = ["pov", "Ада"]
    storage._write_json(root / "state.json", state)


def test_unknown_named_npc_cannot_persist_across_turns_without_card():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        payload = {
            "user_input": "(остаться рядом)",
            "scene_output": "Ада остаётся рядом.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [],
                "presence_updates": [],
                "state_patch": {},
            },
        }

        with pytest.raises(RuntimeError, match="CAST_PERSISTENT_NPC_CARD_REQUIRED"):
            cast_registry_runtime._with_registry_patch(sid, payload)


def test_persistent_named_npc_passes_after_character_upsert():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        payload = {
            "user_input": "(остаться рядом)",
            "scene_output": "Ада остаётся рядом.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [{
                    "character_id": "ada",
                    "name": "Ада",
                    "story_function": "устойчивый участник текущей линии",
                }],
                "presence_updates": [],
                "state_patch": {},
            },
        }

        prepared = cast_registry_runtime._with_registry_patch(sid, payload)
        patch = prepared["extracted"]["state_patch"]["world"]["cast_registry"]
        assert patch["ada"]["origin"] == "story_created"
        assert patch["ada"]["name"] == "Ада"
        assert prepared["extracted"]["state_patch"]["current"]["present_characters"] == ["pov", "ada"]


def test_unregistered_one_off_extra_can_leave_without_becoming_a_card():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)
        root = storage.SESSIONS_DIR / sid

        payload = {
            "extracted": {
                "presence_updates": [{"character_id": "Ада", "action": "leave"}],
                "state_patch": {},
            },
        }
        prepared = scene_presence_runtime._apply_presence_contract(payload, root=root)
        assert prepared["extracted"]["state_patch"]["current"]["present_characters"] == ["pov"]


def test_participating_story_npc_needs_first_relationship_dimension():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        put_unknown_ada_in_persistent_presence(sid)

        base = {
            "user_input": "(ответить Аде)",
            "scene_output": "Кайр отвечает Аде.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [{
                    "character_id": "ada",
                    "name": "Ада",
                    "story_function": "устойчивый участник текущей линии",
                }],
                "relationship_updates": [],
                "state_patch": {},
            },
        }

        with pytest.raises(HTTPException) as exc:
            turn_pipeline._apply_relationship_changes(sid, base)
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "RELATIONSHIP_INITIALIZATION_REQUIRED"

        base["extracted"]["relationship_updates"] = [{
            "character_id": "ada",
            "reason": "содержательное взаимодействие с POV",
            "dimensions": [{"label": "интерес", "value": 1}],
        }]
        prepared = turn_pipeline._apply_relationship_changes(sid, base)
        relation = prepared["_relationships_after"]["npc_to_pov"]["ada"]
        assert relation["dimensions"]["интерес"]["value"] == 1


def test_repeated_unregistered_scene_speaker_requires_card_even_without_presence_state():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid

        prior = {
            "turn_number": 1,
            "user_input": "поговорить",
            "scene_output": "**Ада** — Первый разговор.",
            "extracted": {},
        }
        (root / "turns.jsonl").write_text(
            __import__("json").dumps(prior, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 1
        storage._write_json(root / "meta.json", meta)

        payload = {
            "user_input": "(ответить)",
            "scene_output": "**Ада** — Второй разговор.",
            "extracted": {
                "runtime_rules_reviewed": True,
                "character_upserts": [],
                "presence_updates": [],
                "state_patch": {},
            },
        }

        with pytest.raises(RuntimeError, match="CAST_PERSISTENT_NPC_CARD_REQUIRED"):
            cast_registry_runtime._with_registry_patch(sid, payload)

def test_audit_can_promote_missing_npc_and_keep_original_knowledge_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid

        turns = [
            {
                "turn_number": number,
                "user_input": f"Ход {number}",
                "scene_output": f"Сохранённая сцена {number}.",
                "extracted": {},
            }
            for number in range(1, 16)
        ]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns),
            encoding="utf-8",
        )

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["audit_required"] = True
        meta["last_audit_turn"] = 0
        storage._write_json(root / "meta.json", meta)

        result = turn_pipeline.commit_audit(
            sid,
            {
                "audit_id": "audit-test",
                "start_turn": 1,
                "end_turn": 15,
                "repairs": {
                    "character_upserts": [
                        {
                            "character_id": "ada",
                            "name": "Ада",
                            "story_function": "повторяющийся участник линии Кайра",
                        }
                    ],
                    "knowledge_journal_add": [
                        {
                            "character_id": "ada",
                            "turn": 7,
                            "text": "Кайр отказался отвечать на её вопрос.",
                        }
                    ],
                    "relationship_updates": [
                        {
                            "character_id": "ada",
                            "turn": 8,
                            "reason": "После нескольких разговоров Ада стала заметно больше доверять Кайру.",
                            "dimensions": [{"label": "доверие", "value": 1}],
                        }
                    ],
                    "npc_intent_updates": [
                        {
                            "character_id": "ada",
                            "intent_id": "ask_again",
                            "turn": 9,
                            "summary": "Вернуться к незакрытому вопросу Кайра.",
                        }
                    ],
                    "story_thread_updates": [
                        {
                            "thread_id": "ada_question",
                            "turn": 10,
                            "summary": "Между Адой и Кайром остался незакрытый вопрос.",
                            "participants": ["ada", "pov"],
                        }
                    ],
                    "scene_compactions": [
                        {
                            "start_turn": 1,
                            "end_turn": 15,
                            "summary": "За пятнадцать ходов Ада несколько раз участвовала в одной продолжающейся линии с Кайром, и её участие стало устойчивым.",
                            "status": "open",
                            "participants": ["pov", "ada"],
                            "location": "bar",
                        }
                    ],
                },
                "notes": [],
            },
        )

        assert result["audited_through"] == 15
        cards = storage._read_json(root / "characters.json", [])
        assert any(card.get("character_id") == "ada" for card in cards)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        rows = memory["characters"]["ada"]["knowledge_journal"]
        assert any(
            row.get("turn") == 7 and "отказался отвечать" in row.get("text", "")
            for row in rows
        )

        relationships = relationship_file_runtime.normalize_store(
            storage._read_json(root / relationship_file_runtime.FILE_NAME, {}),
            "pov",
        )
        trust = relationships["npc_to_pov"]["ada"]["dimensions"]["доверие"]
        assert trust["value"] == 1
        assert trust["last_change"]["turn"] == 8
        assert result["relationship_repairs_saved"] == 1

        state = storage._read_json(root / "state.json", {})
        ada_intents = state["npc_intents"]["ada"]
        intent = next(row for row in ada_intents if row["intent_id"] == "ask_again")
        assert intent["created_turn"] == 9
        assert state["threads"]["ada_question"]["created_turn"] == 10
        assert result["continuity_repairs_saved"] == 2
        assert storage._read_json(root / "meta.json", {})["audit_required"] is False

def test_audit_rejects_new_persistent_npc_without_story_function():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["audit_required"] = True
        meta["last_audit_turn"] = 0
        storage._write_json(root / "meta.json", meta)

        with pytest.raises(RuntimeError, match="CAST_STORY_FUNCTION_REQUIRED"):
            turn_pipeline.commit_audit(
                sid,
                {
                    "audit_id": "audit-no-story-function",
                    "start_turn": 1,
                    "end_turn": 15,
                    "repairs": {
                        "character_upserts": [{"character_id": "ada", "name": "Ада"}],
                        "scene_compactions": [
                            {
                                "start_turn": 1,
                                "end_turn": 15,
                                "summary": "За пятнадцать ходов именованная Ада повторно участвовала в линии и стала постоянным персонажем истории.",
                                "status": "open",
                                "participants": ["pov", "ada"],
                                "location": "bar",
                            }
                        ],
                    },
                    "notes": [],
                },
            )


def test_audit_relationship_repair_requires_original_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["audit_required"] = True
        meta["last_audit_turn"] = 0
        storage._write_json(root / "meta.json", meta)

        with pytest.raises(RuntimeError, match="AUDIT_REPAIR_TURN_REQUIRED"):
            turn_pipeline.commit_audit(
                sid,
                {
                    "audit_id": "audit-missing-relation-turn",
                    "start_turn": 1,
                    "end_turn": 15,
                    "repairs": {
                        "character_upserts": [
                            {
                                "character_id": "ada",
                                "name": "Ада",
                                "story_function": "повторяющийся участник линии Кайра",
                            }
                        ],
                        "relationship_updates": [
                            {
                                "character_id": "ada",
                                "reason": "Доказанный сдвиг из audited raw turns.",
                                "dimensions": [{"label": "интерес", "value": 1}],
                            }
                        ],
                        "scene_compactions": [
                            {
                                "start_turn": 1,
                                "end_turn": 15,
                                "summary": "Ада несколько раз участвовала в одной линии с Кайром, и в этой линии возник доказанный сдвиг отношения.",
                                "status": "open",
                                "participants": ["pov", "ada"],
                                "location": "bar",
                            }
                        ],
                    },
                    "notes": [],
                },
            )

def test_fast_audit_reads_canonical_relationships_file_not_legacy_state():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "audit-relationship-source",
            "title": "Audit Relationship Source",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC", "story_function": "постоянный участник"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "present_characters": ["pov", "npc"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        relationships = relationship_file_runtime.normalize_store(
            storage._read_json(root / relationship_file_runtime.FILE_NAME, {}),
            "pov",
        )
        relationships["npc_to_pov"]["npc"] = {
            "dimensions": {
                "доверие": {
                    "value": 7,
                    "last_change": {"turn": 4, "delta": 1, "reason": "реальный сохранённый сдвиг"},
                }
            }
        }
        storage._write_json(root / relationship_file_runtime.FILE_NAME, relationships)

        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {"npc": {"доверие": 99}}
        storage._write_json(root / "state.json", state)

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["last_audit_turn"] = 0
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        payload = fast_audit_runtime._build_fast_payload(sid)
        audit = payload["relationship_audit"]
        assert audit["source"] == "relationships.json"
        assert audit["current_numeric"]["npc"]["доверие"] == 7

def test_fast_audit_scopes_active_continuity_and_does_not_duplicate_cast_registry():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "audit-continuity-scope",
            "title": "Audit Continuity Scope",
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC", "story_function": "постоянный участник"},
            ],
            "starting_state": {
                "pov": {"character_id": "pov"},
                "current": {"location": "room", "present_characters": ["pov", "npc"]},
            },
        }
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["npc_intents"] = {
            "npc": [
                {"intent_id": "open", "character_id": "npc", "summary": "Спросить ещё раз", "status": "active"},
                {"intent_id": "done", "character_id": "npc", "summary": "Уже решено", "status": "resolved"},
            ]
        }
        state["threads"] = {
            "open_thread": {"thread_id": "open_thread", "summary": "Незакрытая встреча", "status": "active"},
            "done_thread": {"thread_id": "done_thread", "summary": "Закрыто", "status": "resolved"},
        }
        state.setdefault("world", {})["cast_registry"] = {
            "npc": {"character_id": "npc", "name": "NPC", "status": "active"}
        }
        storage._write_json(root / "state.json", state)

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["last_audit_turn"] = 0
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        payload = fast_audit_runtime._build_fast_payload(sid)
        assert "npc_intents" not in payload["state_audit"]
        assert "cast_registry" not in payload["state_audit"].get("world", {})
        assert [row["intent_id"] for row in payload["continuity_audit"]["active_npc_intents"]["npc"]] == ["open"]
        assert [row["thread_id"] for row in payload["continuity_audit"]["active_story_thread_index"]] == ["open_thread"]

def test_sixtieth_audit_cannot_skip_macro_compaction():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = make_session()
        root = storage.SESSIONS_DIR / sid
        turns = [
            {
                "turn_number": number,
                "user_input": f"Ход {number}",
                "scene_output": f"Сохранённая сцена {number}.",
                "extracted": {},
            }
            for number in range(1, 61)
        ]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns),
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 60
        meta["last_audit_turn"] = 45
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        with pytest.raises(RuntimeError, match="MACRO_CHRONOLOGY_COMPACTION_REQUIRED"):
            turn_pipeline.commit_audit(
                sid,
                {
                    "audit_id": "audit-60-macro-required",
                    "start_turn": 46,
                    "end_turn": 60,
                    "repairs": {
                        "scene_compactions": [{
                            "start_turn": 46,
                            "end_turn": 60,
                            "summary": "Последние пятнадцать ходов проверены, но обязательная шестидесятиходовая сборка хронологии намеренно не передана.",
                            "status": "open",
                            "participants": ["pov"],
                            "location": "bar",
                        }],
                    },
                    "notes": [],
                },
            )

