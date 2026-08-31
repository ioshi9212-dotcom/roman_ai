import json
import tempfile
from pathlib import Path

from app import session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def reviewed(**extra):
    base = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
    }
    base.update(extra)
    return base


def read_packet(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = []
    for index in range(manifest["chunk_count"]):
        chunk = storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)
        parts.append(chunk["content"])
    return manifest, json.loads("".join(parts))


def make_novel():
    return {
        "novel_id": "continuity",
        "title": "Continuity",
        "version": 1,
        "novel": {"pov_character": "elena"},
        "characters": [
            {"character_id": "elena", "name": "Елена", "is_pov": True, "role": "POV"},
            {"character_id": "kai", "name": "Кай", "role": "командир"},
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "elena"},
            "current": {"location": "yard", "present_characters": ["Елена", "Кай"]},
        },
    }


def test_explicit_introduction_survives_new_chat_continuation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id = storage.create_session(make_novel())["session_id"]

        manifest, packet = read_packet(session_id, "Привет.")
        assert {row["character_id"] for row in packet["character_registry"]} == {"elena", "kai"}
        assert packet["scene_state"]["current"]["present_characters"] == ["elena", "kai"]

        result = session_runtime.commit_turn(
            session_id,
            {
                "user_input": "Привет.",
                "scene_output": "Кай представился Елене. Они познакомились и обменялись именами.",
                "extracted": reviewed(
                    state_patch={"current": {"present_characters": ["elena", "kai"]}},
                    chronology=[
                        {"summary": "Елена и Кай познакомились и обменялись именами.", "importance": "anchor"}
                    ],
                    knowledge_add=[
                        {"character_id": "elena", "fact_id": "kai_name", "content": "Мужчину зовут Кай"},
                        {"character_id": "kai", "fact_id": "elena_name", "content": "Девушку зовут Елена"},
                    ],
                ),
            },
        )
        assert result["handoff_required"] is False

        root = storage.SESSIONS_DIR / session_id
        state = storage._read_json(root / "state.json", {})
        assert state["characters"]["kai"]["pov_familiarity"]["status"] == "acquainted"

        meta = storage._read_json(root / "meta.json", {})
        meta["handoff_required"] = True
        storage._write_json(root / "meta.json", meta)

        checkpoint = session_runtime.continue_session(session_id)
        assert checkpoint["session_id"] == session_id
        assert checkpoint["audit_required"] is False
        kai = next(row for row in checkpoint["character_registry"] if row["character_id"] == "kai")
        assert kai["pov_familiarity"]["status"] == "acquainted"
        assert storage._read_json(root / "meta.json", {})["handoff_required"] is False

        state = storage._read_json(root / "state.json", {})
        state["current"]["present_characters"] = ["elena"]
        storage._write_json(root / "state.json", state)
        _, packet2 = read_packet(session_id, "(ждать)")
        kai2 = next(row for row in packet2["character_registry"] if row["character_id"] == "kai")
        assert kai2["pov_familiarity"]["status"] == "acquainted"
        assert "Never stage a first introduction" in packet2["character_registry_instruction"]


def test_shared_scene_without_identity_is_only_encountered():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id = storage.create_session(make_novel())["session_id"]
        read_packet(session_id, "(посмотреть на мужчину)")
        session_runtime.commit_turn(
            session_id,
            {
                "user_input": "(посмотреть на мужчину)",
                "scene_output": "Мужчина молча прошёл мимо. Имя никто не называл.",
                "extracted": reviewed(state_patch={"current": {"present_characters": ["elena", "kai"]}}),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / session_id / "state.json", {})
        assert state["characters"]["kai"]["pov_familiarity"]["status"] == "encountered"


def test_named_runtime_npc_is_added_to_live_registry():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id = storage.create_session(make_novel())["session_id"]
        read_packet(session_id, "(войти в кофейню)")
        session_runtime.commit_turn(
            session_id,
            {
                "user_input": "(войти в кофейню)",
                "scene_output": "Бариста представилась как Мара.",
                "extracted": reviewed(
                    character_upserts=[
                        {"character_id": "mara", "name": "Мара", "role": "бариста, повторяющийся NPC"}
                    ],
                    state_patch={"current": {"present_characters": ["elena", "mara"]}},
                    chronology=[{"summary": "Елена познакомилась с Марой.", "importance": "anchor"}],
                    knowledge_add=[
                        {"character_id": "elena", "fact_id": "mara_name", "content": "Бариста зовут Мара"}
                    ],
                ),
            },
        )
        checkpoint = session_runtime.continue_session(session_id)
        mara = next(row for row in checkpoint["character_registry"] if row["character_id"] == "mara")
        assert mara["name"] == "Мара"
        assert "бариста" in mara["role"]
        assert mara["pov_familiarity"]["status"] == "acquainted"


def test_turn_sixty_no_longer_creates_handoff_gate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id = storage.create_session(make_novel())["session_id"]
        root = storage.SESSIONS_DIR / session_id
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 59
        meta["last_audit_turn"] = 45
        meta["audit_required"] = False
        meta["handoff_required"] = False
        storage._write_json(root / "meta.json", meta)

        manifest, _ = read_packet(session_id, "ход 60")
        result = session_runtime.commit_turn(
            session_id,
            {"user_input": "ход 60", "scene_output": "scene 60", "extracted": reviewed()},
        )
        assert manifest["prepared_for_turn"] == 60
        assert result["turn_number"] == 60
        assert result["audit_due"] is True
        assert result["handoff_required"] is False
        assert storage._read_json(root / "meta.json", {})["handoff_required"] is False
