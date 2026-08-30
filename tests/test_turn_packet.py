import json
import tempfile
from pathlib import Path

from app import storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def read_packet_context(session_id: str, packet: dict) -> dict:
    parts = []
    for index in range(packet["chunk_count"]):
        chunk = storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)
        parts.append(chunk["content"])
    return json.loads("".join(parts))


def test_packet_must_be_fully_read_before_commit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "packet_test",
            "title": "Packet Test",
            "version": 1,
            "novel": {"rules": ["test rule"], "pov_character": "rina"},
            "lore": {"secret": "hidden"},
            "characters": [
                {"character_id": "rina", "name": "Рина"},
                {"character_id": "aiden", "name": "Эйден"},
                {"character_id": "liam", "name": "Лиам"},
            ],
        }
        storage.save_novel(novel)
        meta = storage.create_session(novel)
        sid = meta["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["pov"]["character_id"] = "rina"
        state["current"]["present_characters"] = ["aiden"]
        storage._write_json(root / "state.json", state)

        user_input = "Посмотреть на Лиама и спросить, где он был."
        packet = storage.prepare_turn_packet(sid, user_input)
        assert "rina" in packet["relevant_character_ids"]
        assert "aiden" in packet["relevant_character_ids"]
        assert "liam" in packet["relevant_character_ids"]

        try:
            storage.commit_turn(sid, {"user_input": user_input, "scene_output": "scene", "extracted": {}})
            assert False, "commit must fail before chunks are read"
        except RuntimeError as exc:
            assert str(exc) == "TURN_PACKET_INCOMPLETE"

        context = read_packet_context(sid, packet)
        assert context["packet_version"] == 3
        assert context["scene_characters"]["aiden"]["present_at_turn_start"] is True
        assert context["scene_characters"]["liam"]["present_at_turn_start"] is False

        result = storage.commit_turn(
            sid,
            {
                "user_input": user_input,
                "scene_output": "scene",
                "extracted": {
                    "knowledge_add": [
                        {"character_id": "liam", "fact_id": "f1", "content": "Rina asked where he was", "source": "Rina"}
                    ]
                },
            },
        )
        assert result["turn_number"] == 1
        memory = storage.get_character_memory(sid, "liam")
        assert memory["knowledge"][0]["fact_id"] == "f1"
        assert not (root / "turn_packet.json").exists()


def test_chronology_is_author_only_and_does_not_become_absent_npc_memory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "knowledge_test",
            "title": "Knowledge Test",
            "version": 1,
            "novel": {"pov_character": "emily"},
            "characters": [
                {"character_id": "emily", "name": "Эмили"},
                {"character_id": "chloe", "name": "Хлоя"},
                {"character_id": "ethan", "name": "Итан"},
            ],
        }
        meta = storage.create_session(novel)
        sid = meta["session_id"]
        root = storage.SESSIONS_DIR / sid

        state = storage._read_json(root / "state.json", {})
        state["pov"] = {"character_id": "emily"}
        state["current"]["present_characters"] = ["emily", "chloe"]
        storage._write_json(root / "state.json", state)

        storage._write_json(
            root / "chronology.json",
            [
                {
                    "turn": 4,
                    "event": "Эмили сказала Хлое, что Рен — владелец найденной зажигалки",
                    "participants": ["emily", "chloe"],
                }
            ],
        )
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        storage._memory_bucket(memory, "emily")["knowledge"].append(
            {"fact_id": "lighter_owner", "content": "Рен владелец зажигалки", "source": "лично установила", "learned_turn": 3}
        )
        storage._memory_bucket(memory, "chloe")["knowledge"].append(
            {"fact_id": "lighter_owner", "content": "Рен владелец зажигалки", "source": "Эмили", "learned_turn": 4}
        )
        storage._write_json(root / "memory.json", memory)

        packet = storage.prepare_turn_packet(sid, "Итан приезжает к кофейне.")
        context = read_packet_context(sid, packet)

        assert "chronology_recent" not in context
        assert context["author_context"]["chronology_recent"][0]["event"].startswith("Эмили сказала Хлое")
        assert context["scene_characters"]["emily"]["personal_memory"]["knowledge"][0]["fact_id"] == "lighter_owner"
        assert context["scene_characters"]["chloe"]["personal_memory"]["knowledge"][0]["fact_id"] == "lighter_owner"
        assert context["scene_characters"]["ethan"]["personal_memory"]["knowledge"] == []
        assert "World history is not character knowledge" in context["knowledge_boundary"]["rule"]
        assert "If someone arrives later" in context["knowledge_boundary"]["instruction"]


def test_character_gets_fact_only_after_explicit_memory_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = {
            "novel_id": "learn_test",
            "title": "Learn Test",
            "version": 1,
            "novel": {"pov_character": "emily"},
            "characters": [
                {"character_id": "emily", "name": "Эмили"},
                {"character_id": "ethan", "name": "Итан"},
            ],
        }
        meta = storage.create_session(novel)
        sid = meta["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["pov"] = {"character_id": "emily"}
        state["current"]["present_characters"] = ["emily", "ethan"]
        storage._write_json(root / "state.json", state)

        first_input = "Эмили говорит Итану: Рен владелец той зажигалки."
        first = storage.prepare_turn_packet(sid, first_input)
        read_packet_context(sid, first)
        storage.commit_turn(
            sid,
            {
                "user_input": first_input,
                "scene_output": "Эмили сообщает это Итану.",
                "extracted": {
                    "knowledge_add": [
                        {"character_id": "ethan", "fact_id": "lighter_owner", "content": "Рен владелец зажигалки", "source": "Эмили"}
                    ],
                    "experiences_add": [
                        {"character_id": "ethan", "event_id": "heard_lighter_owner", "summary": "Эмили сказала, что Рен владелец зажигалки", "role": "heard"}
                    ],
                },
            },
        )

        second = storage.prepare_turn_packet(sid, "Итан отвечает.")
        context = read_packet_context(sid, second)
        ethan_memory = context["scene_characters"]["ethan"]["personal_memory"]
        assert ethan_memory["knowledge"][0]["fact_id"] == "lighter_owner"
        assert ethan_memory["experiences"][0]["event_id"] == "heard_lighter_owner"
