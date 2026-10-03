import json
import tempfile
from pathlib import Path

from app import session_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def relationship_novel():
    return {
        "novel_id": "starting-rel",
        "title": "Starting Relationship",
        "version": 5,
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "relationships": [
                    {
                        "target_character_id": "rina",
                        "relationship_type": "близкая дружба и скрытая влюблённость",
                        "relationship_context": "Давно дружат; Эдриан давно влюблён в Ринату.",
                        "current_dynamic": "Скрывает чувства и остро реагирует на романтических конкурентов.",
                        "beliefs_about_target": ["Рината считает его близким другом."],
                        "unresolved_between_them": ["Эдриан не признался в любви."],
                        "dimensions": [
                            {"label": "близость", "value": 72},
                            {"label": "привязанность", "value": 81},
                            {"label": "влечение", "value": 68},
                            {"label": "ревность", "value": 36},
                        ],
                    }
                ],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "02.04.2026",
                "time": "07:00",
                "location": "home",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def read_context(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "(посмотреть на Эдриана)")
    parts = [
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"]
        for index in range(manifest["chunk_count"])
    ]
    return json.loads("".join(parts))


def test_profile_defined_npc_to_pov_relationship_exists_before_turn_one():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})

        assert state["relationships"]["adrian"] == {
            "близость": 72,
            "привязанность": 81,
            "влечение": 68,
            "ревность": 36,
        }
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["target_character_id"] == "rina"
        assert relation["relationship_type"] == "близкая дружба и скрытая влюблённость"
        assert "Скрывает чувства" in relation["current_dynamic"]
        assert relation["last_changed_turn"] == 0
        assert relation["starting_source"] == "character_profile"

        context = read_context(sid)
        row = next(
            item for item in context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        values = {item["label"]: item["value"] for item in row["dimensions"]}
        assert values["привязанность"] == 81
        assert values["влечение"] == 68
        assert row["relationship_type"] == "близкая дружба и скрытая влюблённость"


def test_explicit_starting_state_numbers_win_over_profile_seed():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = relationship_novel()
        novel["starting_state"]["relationships"] = {
            "adrian": {"близость": 60, "привязанность": 75}
        }
        sid = storage.create_session(novel)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state["relationships"]["adrian"]["близость"] == 60
        assert state["relationships"]["adrian"]["привязанность"] == 75
        assert "влечение" not in state["relationships"]["adrian"]
        relation = state["relationship_documents"]["adrian"]["relations"][0]
        assert relation["relationship_type"] == "близкая дружба и скрытая влюблённость"


def test_free_text_profile_relationship_is_not_guessed_into_numeric_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = relationship_novel()
        novel["characters"][1]["relationships"] = "Эдриан давно влюблён в Ринату."
        sid = storage.create_session(novel)["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})

        assert state.get("relationships", {}).get("adrian", {}) == {}


def test_scene_builder_requires_pov_presence_visual_anchoring_and_no_early_cut():
    builder = Path("runtime/scene_builder.md").read_text(encoding="utf-8")
    instructions = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    rules = Path("runtime/rules.md").read_text(encoding="utf-8")

    assert "POV должен оставаться физически и речево присутствующим" in builder
    assert "Диалог не должен превращаться в радиопьесу" in builder
    assert "не закончена ли она раньше ближайшего реального выбора POV" in builder
    assert "сцена не оборвана сразу после user_input" in instructions
    assert "changed=false" in instructions
    assert "не выбирай по умолчанию" in instructions
    assert "changed=false" in rules
    assert "безопасное значение по умолчанию" in rules
    assert len(instructions) < 8000


def test_small_relationship_delta_persists_and_is_visible_next_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]

        manifest = prepare_turn_request(
            sid,
            "Ты ревнуешь?",
            request_id="small-rel-shift",
        )
        start = 1 if manifest.get("first_chunk_included") else 0
        for index in range(start, manifest["chunk_count"]):
            storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Ты ревнуешь?",
                "scene_output": (
                    "🎭 Starting Relationship · весна\n"
                    "Сцена. Эдриан впервые выдаёт ревность заметной реакцией.\n\n"
                    "Состояние: напряжение\n"
                    "Отношения:\n"
                    "Эдриан - близость 72; привязанность 81; влечение 68; ревность 37/+1\n\n"
                    "Ход 1 · цикл 1/15"
                ),
                "extracted": {
                    "scene_builder_reviewed": True,
                    "persistence_reviewed": True,
                    "knowledge_reviewed": True,
                    "relationship_reviewed": True,
                    "relationship_review": [
                        {
                            "character_id": "adrian",
                            "changed": True,
                            "reason": "Эдриан впервые открыто выдал ревность в разговоре с POV.",
                        }
                    ],
                    "chronology": [],
                    "knowledge_add": [],
                    "knowledge_journal_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "npc_intent_updates": [],
                    "npc_relationship_updates": [],
                    "story_thread_updates": [],
                    "presence_updates": [],
                    "relationship_updates": [
                        {
                            "character_id": "adrian",
                            "reason": "Эдриан впервые открыто выдал ревность в разговоре с POV.",
                            "dimensions": [{"label": "ревность", "delta": 1}],
                        }
                    ],
                    "state_patch": {},
                    "character_upserts": [],
                },
            },
        )
        assert result["already_committed"] is False

        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["adrian"]["ревность"] == 37

        next_context = read_context(sid)
        row = next(
            item for item in next_context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        values = {item["label"]: item["value"] for item in row["dimensions"]}
        assert values["ревность"] == 37
