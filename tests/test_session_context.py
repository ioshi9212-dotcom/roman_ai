import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from tests.helpers import commit_with_packet


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_packet_context(session_id: str, user_input: str):
    packet = session_runtime.prepare_turn_packet(session_id, user_input)
    text = ""
    for index in range(packet["chunk_count"]):
        text += storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)["content"]
    return packet, json.loads(text)


def test_starting_state_full_canon_and_cast_are_available():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "context_test",
            "title": "Context Test",
            "version": 1,
            "novel": {"pov_character": "elena", "genre": "romance"},
            "rules": {"tone": "cinematic"},
            "lore": {"public": "east and west are divided"},
            "hidden_lore": {"secret": "Aiden knows the archive exists"},
            "world": {"city": "Eastern Sector"},
            "story_direction": {"focus": "relationships first"},
            "characters": [
                {"character_id": "elena", "name": "Elena", "is_pov": True, "role": "POV"},
                {"character_id": "aiden", "name": "Aiden", "role": "commander", "past": "lost his former team"},
                {"character_id": "liam", "name": "Liam", "role": "commander"},
            ],
            "starting_state": {
                "current": {"location": "training hall", "present_characters": ["elena", "aiden"]},
                "relationships": {"aiden": {"trust": 10}},
            },
        }
        meta = storage.create_session(novel)
        sid = meta["session_id"]
        session = storage.load_session(sid)
        assert session["state"]["pov"]["character_id"] == "elena"
        assert session["state"]["current"]["location"] == "training hall"
        assert len(session["characters"]) == 3

        packet, context = read_packet_context(sid, "Начать стартовую сцену")
        assert "elena" in packet["relevant_character_ids"]
        assert "aiden" in packet["relevant_character_ids"]
        assert {x["character_id"] for x in context["character_cards"]} == {"elena", "aiden"}
        assert {x["character_id"] for x in context["cast_index"]} == {"elena", "aiden", "liam"}
        assert {x["character_id"] for x in context["character_registry"]} == {"elena", "aiden", "liam"}
        assert context["novel_rules"]["tone"] == "cinematic"
        assert context["hidden_lore"]["secret"].startswith("Aiden")
        assert context["story_direction"]["focus"] == "relationships first"
        assert context["world_canon"]["city"] == "Eastern Sector"


def test_new_recurring_npc_becomes_live_card_and_returns_later():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "npc_test",
            "title": "NPC Test",
            "novel": {"pov_character": "rina"},
            "characters": [{"character_id": "rina", "name": "Rina", "is_pov": True}],
            "lore": {},
            "starting_state": {"current": {"location": "bar", "present_characters": ["rina"]}},
        }
        sid = storage.create_session(novel)["session_id"]

        commit_with_packet(
            sid,
            "A stranger introduces himself as Knox.",
            "Knox enters the story.",
            {
                "character_upserts": [
                    {"character_id": "knox", "name": "Knox", "role": "recurring ally", "past": "former scout"}
                ],
                "state_patch": {"current": {"present_characters": ["rina", "knox"]}},
                "experiences_add": [
                    {"character_id": "knox", "event_id": "met_rina", "summary": "Knox met Rina"}
                ],
            },
        )

        session = storage.load_session(sid)
        assert any(x["character_id"] == "knox" for x in session["characters"])
        assert session["memory"]["characters"]["knox"]["experiences"][0]["event_id"] == "met_rina"

        packet, context = read_packet_context(sid, "Посмотреть на Нокса")
        assert "knox" in packet["relevant_character_ids"]
        knox = next(x for x in context["character_cards"] if x["character_id"] == "knox")
        assert knox["card"]["past"] == "former scout"
        registry_knox = next(x for x in context["character_registry"] if x["character_id"] == "knox")
        assert registry_knox["name"] == "Knox"
