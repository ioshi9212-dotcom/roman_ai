import json
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from app import relationship_file_runtime, session_runtime, simple_setup_runtime, storage
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
        "lore": {},
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
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def read_relationships(session_id: str):
    return storage._read_json(storage.SESSIONS_DIR / session_id / "relationships.json", {})


def read_all_pending_chunks(session_id: str, manifest):
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def test_profile_relationship_is_canon_in_relationships_file_before_turn_one():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        store = read_relationships(sid)

        assert "relationships" not in state
        assert "relationship_documents" not in state
        assert "npc_relationships" not in state

        dims = store["npc_to_pov"]["adrian"]["dimensions"]
        assert {label: item["value"] for label, item in dims.items()} == {
            "близость": 72,
            "привязанность": 81,
            "влечение": 68,
            "ревность": 36,
        }
        assert dims["влечение"]["last_change"]["turn"] == 0
        assert "анк" in dims["влечение"]["last_change"]["reason"].casefold() or dims["влечение"]["last_change"]["reason"]

        context = read_context(sid)
        row = next(
            item for item in context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        values = {item["label"]: item["value"] for item in row["dimensions"]}
        assert values["привязанность"] == 81
        assert values["влечение"] == 68
        assert context["relationship_lens"]["source"] == "relationships.json"



def test_legacy_session_migrates_once_to_relationships_file_and_drops_invalid_negative_values():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        (root / "relationships.json").unlink()

        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {"adrian": {"доверие": 17, "раздражение": -4}}
        state["relationship_documents"] = {
            "adrian": {
                "owner_character_id": "adrian",
                "relations": [
                    {
                        "target_character_id": "rina",
                        "change_reasons": [
                            {
                                "reason": "Рината поймала его на лжи.",
                                "changes": [{"label": "доверие", "from": 20, "to": 17}],
                            }
                        ],
                    }
                ],
            }
        }
        storage._write_json(root / "state.json", state)

        cards = storage._load_cards(root, storage._read_json(root / "source.json", {}))
        migrated = relationship_file_runtime.load(
            root,
            cards=cards,
            state=state,
            pov_id="rina",
        )

        dims = migrated["npc_to_pov"]["adrian"]["dimensions"]
        assert dims["доверие"]["value"] == 17
        assert "раздражение" not in dims
        cleaned = storage._read_json(root / "state.json", {})
        assert "relationships" not in cleaned
        assert "relationship_documents" not in cleaned


def test_first_legacy_prepare_exposes_only_positive_relationship_values():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        (root / "relationships.json").unlink()

        state = storage._read_json(root / "state.json", {})
        state["relationships"] = {"adrian": {"доверие": 17, "раздражение": -4}}
        state["relationship_documents"] = {
            "adrian": {
                "owner_character_id": "adrian",
                "relations": [{"target_character_id": "rina", "relationship_type": "напряжённая дружба"}],
            }
        }
        storage._write_json(root / "state.json", state)

        context = read_context(sid)
        row = next(
            item for item in context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        values = {item["label"]: item["value"] for item in row["dimensions"]}
        assert values["доверие"] == 17
        assert "раздражение" not in values

        legacy_keys = {
            "relationships",
            "relationship_documents",
            "relationship_schemas",
            "npc_relationships",
        }
        assert legacy_keys.isdisjoint(context)
        assert legacy_keys.isdisjoint(context.get("scene_state", {}))
        assert legacy_keys.isdisjoint(context.get("author_context", {}))
        for lens in context.get("scene_characters", {}).values():
            assert "relationship_to_pov" not in lens

        cleaned = storage._read_json(root / "state.json", {})
        assert legacy_keys.isdisjoint(cleaned)
        saved_dims = read_relationships(sid)["npc_to_pov"]["adrian"]["dimensions"]
        assert saved_dims["доверие"]["value"] == 17
        assert "раздражение" not in saved_dims

def test_scene_builder_keeps_pov_visible_without_forcing_speech():
    builder = Path("runtime/scene_builder.md").read_text(encoding="utf-8")
    instructions = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "POV должен оставаться живым и наблюдаемым участником сцены" in builder
    assert "Это не означает обязательную речь" in builder
    assert "Диалог не должен превращаться в радиопьесу" in builder
    assert "После user_input POV продолжает жить в сцене." in builder
    assert "Обычная реплика, короткая реакция, шутка, ругань, уточнение, уклончивый ответ и продолжение уже выбранного действия не являются новым значимым решением." in builder
    assert "Обычно сцена может быть около 1800–3000 непробельных символов, но это не лимит и не цель." in builder
    assert "сцена не оборвана сразу после user_input" in instructions
    assert len(instructions) + 93 < 8000

def test_new_scene_npc_without_starting_relationship_is_still_in_relationship_lens():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        template = relationship_novel()
        template["characters"].append({
            "character_id": "tessa",
            "name": "Тэсса",
        })
        template["starting_state"]["current"]["present_characters"] = ["rina", "tessa"]
        sid = storage.create_session(template)["session_id"]

        context = read_context(sid)
        row = next(
            item for item in context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "tessa"
        )
        assert row["dimensions"] == []
        assert "После первого содержательного взаимодействия постоянного NPC с POV" in context["relationship_lens"]["initialization_rule"]
        assert "±1 = небольшой, но реальный сдвиг" in context["relationship_lens"]["small_shift_rule"]


def test_small_everyday_shift_is_valid_without_critical_event():
    cards = relationship_novel()["characters"]
    store = relationship_file_runtime.build_initial_store(
        cards,
        relationship_novel()["starting_state"],
        "rina",
    )

    changed = relationship_file_runtime.apply_updates(
        store,
        [{
            "character_id": "adrian",
            "reason": "Рината выполнила маленькое обещание, и Эдриан чуть меньше сомневается в ней.",
            "dimensions": [{"label": "доверие", "value": 1}],
        }],
        cards=cards,
        pov_id="rina",
        turn_number=2,
        participant_ids=["adrian"],
    )

    assert changed["npc_to_pov"]["adrian"]["dimensions"]["доверие"]["value"] == 1


def test_relationship_rules_do_not_require_major_or_durable_event_for_plus_one():
    rules = Path("runtime/rules.md").read_text(encoding="utf-8")
    instructions = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")

    assert "±1 — небольшой, но заметный сдвиг" in rules
    assert "не требуй доказательства, что он окончательный или долговременный" in rules
    assert "Отдельный отчёт о проверке не нужен" in rules
    assert "changed=false" not in instructions
    assert "±1 малый" in instructions

def test_v5_setup_requires_structured_pre_story_npc_to_pov_relationship():
    template = relationship_novel()
    template["characters"][1]["relationships"] = "Эдриан давно влюблён в Ринату."
    with pytest.raises(ValueError, match="DRAFT_CHARACTER_RELATIONSHIP_STRUCTURE_REQUIRED"):
        simple_setup_runtime._validate_simple_content(template)



def test_v5_setup_uses_positive_values_and_merges_rows_to_same_target():
    template = relationship_novel()
    template["characters"][1]["relationships"] = [
        {
            "target_character_id": "rina",
            "relationship_type": "лучшие друзья",
            "dimensions": [
                {"label": "близость", "value": 74},
                {"label": "настороженность", "value": 12},
            ],
        },
        {
            "target_character_id": "Рината",
            "relationship_type": "скрытая влюблённость",
            "dimensions": [
                {"label": "влечение", "value": 82},
                {"label": "ревность", "value": 35},
            ],
        },
    ]

    normalized, _ = simple_setup_runtime._validate_simple_content(template)
    adrian = next(row for row in normalized["characters"] if row["character_id"] == "adrian")
    assert len(adrian["relationships"]) == 1
    values = {item["label"]: item["value"] for item in adrian["relationships"][0]["dimensions"]}
    assert values == {
        "близость": 74,
        "настороженность": 12,
        "влечение": 82,
        "ревность": 35,
    }

    template["characters"][1]["relationships"][0]["dimensions"][1]["value"] = -1
    with pytest.raises(ValueError, match="DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_INVALID"):
        simple_setup_runtime._validate_simple_content(template)

def test_npc_to_npc_setup_is_qualitative_only():
    template = relationship_novel()
    template["characters"].append({"character_id": "dante", "name": "Данте"})
    template["characters"][1]["relationships"] = [
        {
            "target_character_id": "dante",
            "relationship_type": "лучшие друзья",
            "dimensions": [{"label": "дружба", "value": 70}],
        }
    ]
    with pytest.raises(ValueError, match="DRAFT_NPC_NPC_RELATIONSHIP_MUST_BE_QUALITATIVE"):
        simple_setup_runtime._validate_simple_content(template)


def test_small_delta_persists_only_in_relationships_file_and_is_visible_next_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(relationship_novel())["session_id"]

        manifest = prepare_turn_request(sid, "Ты ревнуешь?", request_id="small-rel-shift")
        read_all_pending_chunks(sid, manifest)

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Ты ревнуешь?",
                "scene_output": (
                    "🎭 Starting Relationship · весна\n"
                    "Эдриан отвечает и заметно выдаёт ревность.\n\n"
                    "Состояние: напряжение\n"
                    "Отношения:\n"
                    "Эдриан - близость 72/0; привязанность 81/0; влечение 68/0; ревность 37/+1\n\n"
                    "Ход 1 · цикл 1/15"
                ),
                "extracted": {
                    "scene_builder_reviewed": True,
                    "persistence_reviewed": True,
                    "knowledge_reviewed": True,
                    "chronology": [],
                    "knowledge_add": [],
                    "knowledge_journal_add": [],
                    "experiences_add": [],
                    "dialogue_memory_add": [],
                    "npc_intent_updates": [],
                    "npc_relationship_updates": [],
                    "story_thread_updates": [],
                    "presence_updates": [],
                    "relationship_review": [
                        {
                            "character_id": "adrian",
                            "changed": True,
                            "reason": "Сцена немного усилила ревность.",
                        }
                    ],
                    "relationship_updates": [
                        {
                            "character_id": "adrian",
                            "reason": "Впервые заметно выдал ревность.",
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
        assert "relationships" not in state
        store = read_relationships(sid)
        assert store["npc_to_pov"]["adrian"]["dimensions"]["ревность"]["value"] == 37
        last = store["npc_to_pov"]["adrian"]["dimensions"]["ревность"]["last_change"]
        assert last == {"turn": 1, "delta": 1, "reason": "Впервые заметно выдал ревность."}

        next_context = read_context(sid)
        row = next(
            item for item in next_context["relationship_lens"]["relations_in_current_scene"]
            if item["owner_character_id"] == "adrian"
        )
        values = {item["label"]: item["value"] for item in row["dimensions"]}
        assert values["ревность"] == 37



def test_zero_removes_dimension_negative_values_are_rejected_and_ordinary_delta_is_bounded():
    cards = relationship_novel()["characters"]
    store = relationship_file_runtime.build_initial_store(
        cards,
        relationship_novel()["starting_state"],
        "rina",
    )
    store["npc_to_pov"]["adrian"]["dimensions"]["тонкое доверие"] = {
        "value": 2,
        "last_change": {"turn": 0, "delta": 0, "reason": "старт"},
    }

    changed = relationship_file_runtime.apply_updates(
        store,
        [{
            "character_id": "adrian",
            "reason": "POV резко отвергла просьбу.",
            "dimensions": [
                {"label": "тонкое доверие", "delta": -3},
                {"label": "ревность", "delta": -3},
            ],
        }],
        cards=cards,
        pov_id="rina",
        turn_number=4,
        participant_ids=["adrian"],
    )
    dims = changed["npc_to_pov"]["adrian"]["dimensions"]
    assert "тонкое доверие" not in dims
    assert dims["ревность"]["value"] == 33
    assert all(1 <= item["value"] <= 100 for item in dims.values())

    with pytest.raises(ValueError, match="RELATIONSHIP_NEW_DIMENSION_VALUE_REQUIRED"):
        relationship_file_runtime.apply_updates(
            changed,
            [{
                "character_id": "adrian",
                "reason": "Новая реакция.",
                "dimensions": [{"label": "обида", "value": -2}],
            }],
            cards=cards,
            pov_id="rina",
            turn_number=5,
            participant_ids=["adrian"],
        )

    with pytest.raises(ValueError, match="RELATIONSHIP_ORDINARY_DELTA_LIMIT"):
        relationship_file_runtime.apply_updates(
            changed,
            [{
                "character_id": "adrian",
                "reason": "Обычная сцена.",
                "dimensions": [{"label": "ревность", "delta": 4}],
            }],
            cards=cards,
            pov_id="rina",
            turn_number=5,
            participant_ids=["adrian"],
        )


def test_critical_event_is_capped_at_100_and_ten_dimension_cap_is_real():
    cards = relationship_novel()["characters"]
    store = relationship_file_runtime.build_initial_store(
        cards,
        relationship_novel()["starting_state"],
        "rina",
    )
    changed = relationship_file_runtime.apply_updates(
        store,
        [{
            "character_id": "adrian",
            "reason": "POV спасла ему жизнь.",
            "change_scale": "critical_event",
            "dimensions": [{"label": "привязанность", "delta": 30}],
        }],
        cards=cards,
        pov_id="rina",
        turn_number=6,
        participant_ids=["adrian"],
    )
    assert changed["npc_to_pov"]["adrian"]["dimensions"]["привязанность"]["value"] == 100

    dims = changed["npc_to_pov"]["adrian"]["dimensions"]
    while len(dims) < 10:
        idx = len(dims)
        dims[f"ось-{idx}"] = {
            "value": 1,
            "last_change": {"turn": 0, "delta": 0, "reason": "test"},
        }

    with pytest.raises(ValueError, match="RELATIONSHIP_DIMENSION_LIMIT"):
        relationship_file_runtime.apply_updates(
            changed,
            [{
                "character_id": "adrian",
                "reason": "Появилась ещё одна реакция.",
                "dimensions": [{"label": "одиннадцатая", "value": 1}],
            }],
            cards=cards,
            pov_id="rina",
            turn_number=7,
            participant_ids=["adrian"],
        )

    freed_label = next(label for label in dims if label.startswith("ось-"))
    replaced = relationship_file_runtime.apply_updates(
        changed,
        [{
            "character_id": "adrian",
            "reason": "Одна реакция исчезла, появилась другая.",
            "dimensions": [
                {"label": "новая ось", "value": 1},
                {"label": freed_label, "delta": -1},
            ],
        }],
        cards=cards,
        pov_id="rina",
        turn_number=8,
        participant_ids=["adrian"],
    )
    replaced_dims = replaced["npc_to_pov"]["adrian"]["dimensions"]
    assert len(replaced_dims) == 10
    assert freed_label not in replaced_dims
    assert replaced_dims["новая ось"]["value"] == 1

