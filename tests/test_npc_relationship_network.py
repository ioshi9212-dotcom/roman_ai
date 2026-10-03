import json
import tempfile
from pathlib import Path

from app import relationship_file_runtime, session_runtime, storage
from app.character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "npc-network",
        "title": "NPC Network",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "rina", "genres": ["romance"]},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "story_function": "romantic lead",
                "goals": ["скрывать ревность"],
                "relationships": [
                    {
                        "target_character_id": "dante",
                        "relationship_type": "лучшие друзья",
                        "relationship_context": "давняя дружба",
                        "current_dynamic": "Эдриан доверяет Данте, но бесится от его провокаций",
                    }
                ],
            },
            {
                "character_id": "dante",
                "name": "Данте",
                "story_function": "friend and provocateur",
                "goals": ["вывести Эдриана на честную реакцию"],
                "relationships": [
                    {
                        "target_character_id": "adrian",
                        "relationship_type": "лучшие друзья",
                        "relationship_context": "знает его много лет",
                        "current_dynamic": "любит намеренно провоцировать Эдриана",
                    }
                ],
            },
            {
                "character_id": "yuna",
                "name": "Юна",
                "relationships": [
                    {
                        "target_character_id": "lem",
                        "relationship_type": "бывшие",
                        "relationship_context": "старые стычки после расставания",
                    }
                ],
            },
            {
                "character_id": "lem",
                "name": "Лем",
                "relationships": [
                    {
                        "target_character_id": "yuna",
                        "relationship_type": "бывшие",
                        "relationship_context": "считает часть старого конфликта закрытой",
                    }
                ],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "01.09.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["rina"],
            },
        },
    }


def read_context(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "(остаться наблюдать)")
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def read_bundle(session_id: str, character_id: str):
    manifest = prepare_character_bundle_read(session_id, character_id)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(
            get_character_bundle_chunk(
                session_id,
                character_id,
                manifest["read_id"],
                index,
            )["content"]
        )
    return json.loads("".join(parts))


def test_every_turn_has_causal_cast_and_qualitative_directed_npc_network_from_same_file():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        context = read_context(sid)

        registry = context["cast_registry"]
        assert registry["mandatory_causal_review"] is True
        assert registry["recency_rotation_disabled"] is True
        rows = {row["character_id"]: row for row in registry["characters"]}
        assert {"adrian", "dante", "yuna", "lem"}.issubset(rows)
        assert registry["bundle_retrieval"]["action"] == "prepareCharacterBundleRead"
        assert all("full_card_retrieval" not in row for row in rows.values())

        network = context["npc_relationship_network"]
        assert network["directional"] is True
        pairs = {
            (row["owner_character_id"], row["target_character_id"]): row["description"]
            for row in network["relations"]
        }
        assert "лучшие друзья" in pairs[("adrian", "dante")]
        assert "бесится" in pairs[("adrian", "dante")]
        assert "провоцировать" in pairs[("dante", "adrian")]
        assert "бывшие" in pairs[("yuna", "lem")]
        assert "бывшие" in pairs[("lem", "yuna")]

        store = storage._read_json(storage.SESSIONS_DIR / sid / "relationships.json", {})
        assert isinstance(store["npc_to_npc"]["adrian"]["dante"], str)
        assert isinstance(store["npc_to_npc"]["dante"]["adrian"], str)


def test_selected_offscreen_bundle_reads_both_relationship_layers_from_relationships_file():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        source = novel()
        source["characters"][2]["relationships"].append({
            "target_character_id": "rina",
            "relationship_type": "интерес",
            "dimensions": [{"label": "интерес", "value": 14}],
        })
        sid = storage.create_session(source)["session_id"]

        bundle = read_bundle(sid, "dante")
        assert bundle["character_id"] == "dante"
        assert bundle["relationship_to_pov"]["dimensions"]["интерес"]["value"] == 14

        outgoing = bundle["npc_relationships_director_only"]
        row = next(item for item in outgoing if item["target_character_id"] == "adrian")
        assert "провоцировать" in row["description"]
        assert "current_dynamic" not in row


def test_npc_to_npc_update_changes_only_requested_direction_and_stays_qualitative():
    cards = novel()["characters"]
    store = relationship_file_runtime.build_initial_store(
        cards,
        novel()["starting_state"],
        "rina",
    )
    changed = relationship_file_runtime.apply_npc_updates(
        store,
        [{
            "owner_character_id": "adrian",
            "target_character_id": "dante",
            "description": "Лучший друг. После ссоры злится и временно избегает его.",
            "change_reason": "Данте намеренно довёл его провокацией.",
        }],
        cards=cards,
        pov_id="rina",
    )

    assert "избегает" in changed["npc_to_npc"]["adrian"]["dante"]
    assert "провоцировать" in changed["npc_to_npc"]["dante"]["adrian"]
    assert isinstance(changed["npc_to_npc"]["adrian"]["dante"], str)


def test_relationship_contract_keeps_npc_to_npc_qualitative_and_footer_scene_scoped():
    rules = Path("runtime/rules.md").read_text(encoding="utf-8")
    builder = Path("runtime/scene_builder.md").read_text(encoding="utf-8")
    instructions = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    schema = Path("openapi.yaml").read_text(encoding="utf-8")

    assert "Единственный канон отношений - `relationships.json`." in rules
    assert "Связи NPC между собой" in rules
    assert "только словами, без числовых шкал" in rules
    assert "только физически присутствующих NPC" in rules
    assert "Footer показывает все активные оси только физически присутствующих NPC" in instructions
    assert "все его активные NPC→POV показатели из relationships.json" in builder
    assert "description:" in schema
    assert "relationship_review:" not in schema
    assert len(instructions) + 93 < 8000
