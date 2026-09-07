import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.character_access import get_character_bundle


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "npc-intents",
        "title": "NPC Intents",
        "novel": {"pov_character": "pov"},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True},
            {"character_id": "ren", "name": "Ren", "traits": ["stubborn", "suspicious"]},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {"location": "home", "scene": "alone", "present_characters": ["pov"], "game_day": 1},
        },
    }


def read_remaining(manifest):
    for index in range(1 if manifest.get("first_chunk_included") else 0, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(manifest["session_id"] if "session_id" in manifest else SID, manifest["packet_id"], index)


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    pieces = [manifest["content"]] if manifest.get("first_chunk_included") else []
    start = 1 if pieces else 0
    for index in range(start, manifest["chunk_count"]):
        pieces.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(pieces))


def test_unresolved_npc_intent_persists_and_resurfaces_without_player_reminder():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest, _ = read_packet(sid, "(убрать телефон)")
        result = session_runtime.commit_turn(
            sid,
            {
                "user_input": "(убрать телефон)",
                "scene_output": "POV remains alone.\n\nОтношения:\n\nХод 1 · цикл 1/15",
                "extracted": {
                    "persistence_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "npc_intent_updates": [
                        {
                            "character_id": "ren",
                            "intent_id": "check_account_origin",
                            "kind": "investigation",
                            "summary": "Выяснить, кто стоит за странным аккаунтом",
                            "priority": "high",
                            "planned_action": "Проверить происхождение аккаунта и потом вернуться к POV с результатом",
                            "next_eligible_game_day": 3,
                        }
                    ],
                },
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(root / "state.json", {})
        saved = state["npc_intents"]["ren"][0]
        assert saved["intent_id"] == "check_account_origin"
        assert saved["status"] == "active"

        state["current"]["game_day"] = 5
        state["current"]["present_characters"] = ["pov", "ren"]
        state.setdefault("characters", {}).setdefault("ren", {})["present"] = True
        storage._write_json(root / "state.json", state)

        manifest2, context = read_packet(sid, "(посмотреть на Рена)")
        intent = context["npc_active_intents"]["ren"][0]
        assert intent["intent_id"] == "check_account_origin"
        assert intent["eligible_now"] is True
        assert "напомин" in context["npc_intent_instruction"].casefold() or "remind" in context["npc_intent_instruction"].casefold()

        bundle = get_character_bundle(sid, "ren")
        assert bundle["active_intents"][0]["intent_id"] == "check_account_origin"


def test_intent_can_be_marked_pursued_and_resolved():
    from app.npc_intent import apply_updates, active_intents_for

    state = {
        "current": {"game_day": 4},
        "npc_intents": {
            "ren": [
                {
                    "intent_id": "ask_again",
                    "character_id": "ren",
                    "summary": "Вернуться к незакрытому вопросу",
                    "status": "active",
                    "priority": 70,
                    "created_turn": 10,
                    "created_game_day": 1,
                }
            ]
        },
    }
    pursued = apply_updates(
        state,
        [{"character_id": "ren", "intent_id": "ask_again", "pursued_now": True}],
        current_turn=20,
    )
    item = pursued["npc_intents"]["ren"][0]
    assert item["last_pursued_turn"] == 20
    assert item["last_pursued_game_day"] == 4

    resolved = apply_updates(
        pursued,
        [{"character_id": "ren", "intent_id": "ask_again", "operation": "resolve", "resolution": "получил ответ"}],
        current_turn=21,
    )
    assert resolved["npc_intents"]["ren"][0]["status"] == "resolved"
    assert active_intents_for(resolved, ["ren"], current_turn=30) == {}
