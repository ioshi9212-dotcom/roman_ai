import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.living_world_runtime import RELATIONSHIP_DIMENSIONS


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel(*, present=True, foundation=None):
    current_present = ["rina", "adrian"] if present else ["rina"]
    template = {
        "novel_id": "living-world",
        "title": "Living World",
        "novel": {"pov_character": "rina", "genres": ["триллер", "экшн"]},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "personality": {"traits": ["упрямый", "ревнивый"]},
                "goals": ["выяснять то, что считает важным"],
            },
        ],
        "lore": {},
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {"location": "room", "present_characters": current_present},
            "relationships": {"adrian": {"доверие": 80, "близость": 70}},
            "relationship_documents": {
                "adrian": {
                    "owner_character_id": "adrian",
                    "relations": [{
                        "target_character_id": "rina",
                        "relationship_type": "близкая связь",
                        "relationship_context": "",
                        "current_dynamic": "Считает Ринату упрямой и склонной уходить от неудобных ответов.",
                        "dimensions": [
                            {"key": "доверие", "label": "доверие", "value": 80},
                            {"key": "близость", "label": "близость", "value": 70},
                        ],
                        "beliefs_about_target": [{"belief": "она часто отшучивается", "confidence": 0.8}],
                        "unresolved_between_them": ["неполученный ответ"],
                        "last_changed_turn": 0,
                    }],
                }
            },
        },
    }
    if foundation is not None:
        template["foundation"] = foundation
    return template


def read_packet(sid: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    chunks = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        chunks.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
    return json.loads("".join(chunks))


def extracted(**extra):
    data = {
        "persistence_reviewed": True,
        "chronology": [],
        "knowledge_add": [],
        "experiences_add": [],
        "dialogue_memory_add": [],
        "npc_intent_updates": [],
        "story_thread_updates": [],
    }
    data.update(extra)
    return data


def scene(turn=1):
    return f"🎭 Test\n\nСцена.\n\nСостояние: нормально\nОтношения:\nЭдриан - доверие 80; близость 70\n\nХод {turn} · цикл {turn}/15"


def test_packet_builds_actor_frame_from_character_relationship_opinion_and_unresolved_state():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        packet = read_packet(sid, "Пожать плечами")
        living = packet["living_world"]
        assert living["mandatory"] is True
        assert set(living["relationship_model"]["fixed_new_dimensions"]) == set(RELATIONSHIP_DIMENSIONS)
        frame = next(row for row in living["npc_actor_frames"] if row["character_id"] == "adrian")
        assert "упрямый" in json.dumps(frame["character_drivers"], ensure_ascii=False)
        assert "уходить от неудобных ответов" in frame["relationship"]["current_opinion"]
        assert frame["relationship"]["beliefs_about_pov"]
        assert frame["relationship"]["unresolved_between_them"] == ["неполученный ответ"]


def test_strong_relationship_can_surface_absent_npc_in_cast_pressure():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel(present=False))["session_id"]
        packet = read_packet(sid, "Сесть у окна")
        pressure = packet["narrative_guardrails"]["cast_pressure"]
        adrian = next(row for row in pressure if row.get("character_id") == "adrian")
        assert adrian["relationship_salience"] >= 0.6


def test_present_npc_opinion_can_change_without_fake_numeric_relationship_update():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "Ответить")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "Ответить",
                "scene_output": scene(),
                "extracted": extracted(
                    relationship_updates=[{
                        "character_id": "adrian",
                        "reason": "Ответ Ринаты дал Эдриану конкретное основание считать, что она намеренно скрывает часть правды.",
                        "change_scale": "ordinary",
                        "opinion": "Теперь считает, что Рината скрывает что-то намеренно.",
                        "beliefs_about_target": [{"belief": "она скрывает часть правды", "confidence": 0.9}],
                        "unresolved_between_them": ["добиться полного ответа"],
                    }]
                ),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["current_dynamic"] == "Теперь считает, что Рината скрывает что-то намеренно."
        assert relation["beliefs_about_target"][0]["belief"] == "она скрывает часть правды"
        assert relation["unresolved_between_them"] == ["добиться полного ответа"]
        assert state["relationships"]["adrian"] == {"доверие": 80, "близость": 70}


def test_social_effect_from_chronology_becomes_persistent_world_signal():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        read_packet(sid, "Сказать громко")
        session_runtime.commit_turn(
            sid,
            {
                "user_input": "Сказать громко",
                "scene_output": scene(),
                "extracted": extracted(chronology=[{
                    "event": "Разговор услышали люди рядом.",
                    "social_effect": {"scope": "корпус", "impression": "POV заметили и обсуждают"},
                }]),
            },
        )
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        signals = state["world"]["social"]["signals"]
        assert signals
        assert next(iter(signals.values()))["scope"] == "корпус"
