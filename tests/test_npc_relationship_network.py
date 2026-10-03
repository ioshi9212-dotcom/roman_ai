import json
import tempfile
from pathlib import Path

from app import npc_relationship_runtime, session_runtime, storage
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
                "work": "школа",
                "residence": "дом",
                "relationships": [
                    {
                        "target_character_id": "dante",
                        "relationship_type": "лучшие друзья",
                        "relationship_context": "давняя дружба; Данте знает, что Эдриан влюблён в Ринату",
                        "current_dynamic": "Эдриан доверяет Данте, но бесится от его провокаций",
                        "unresolved_between_them": ["Данте регулярно цепляет его ревность"],
                    }
                ],
            },
            {
                "character_id": "dante",
                "name": "Данте",
                "story_function": "friend and provocateur",
                "goals": ["вывести Эдриана на честную реакцию"],
                "work": "школа",
                "residence": "дом",
                "relationships": [
                    {
                        "target_character_id": "adrian",
                        "relationship_type": "лучшие друзья",
                        "relationship_context": "знает о чувствах Эдриана к Ринате",
                        "current_dynamic": "любит намеренно провоцировать Эдриана на ревность",
                        "behavioral_pattern": "поддевает и флиртует рядом с Ринатой, чтобы посмотреть на реакцию Эдриана",
                        "interaction_hooks": ["ревность Эдриана", "старые дружеские подколы"],
                    }
                ],
            },
            {
                "character_id": "yuna",
                "name": "Юна",
                "story_function": "secondary cast",
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
                "story_function": "secondary cast",
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


def test_every_turn_has_one_full_causal_cast_registry_and_directed_npc_network():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        context = read_context(sid)

        assert "cast_index" not in context
        registry = context["cast_registry"]
        assert registry["mandatory_causal_review"] is True
        assert registry["recency_rotation_disabled"] is True

        rows = {row["character_id"]: row for row in registry["characters"]}
        assert {"adrian", "dante", "yuna", "lem"}.issubset(rows)
        assert rows["dante"]["story_function"] == "friend and provocateur"
        assert rows["dante"]["goals"]
        assert rows["dante"]["full_card_retrieval"] == {
            "action": "prepareCharacterBundleRead",
            "character_id": "dante",
            "then": "read all getCharacterBundleChunk chunks before participation",
        }
        assert "known_relationships" not in rows["dante"]
        assert "npc_relationships" not in rows["dante"]
        assert "work" not in rows["dante"]
        assert "residence" not in rows["dante"]
        assert {
            (item["direction"], item["other_character_id"])
            for item in rows["dante"]["npc_relation_refs"]
        } >= {("outgoing", "adrian"), ("incoming", "adrian")}

        forbidden = {
            "turns_since_physical",
            "game_days_since_physical",
            "turns_since_contact",
            "turns_since_meaningful",
            "appearance_count",
            "last_seen_turn",
            "last_interaction_turn",
        }
        assert forbidden.isdisjoint(rows["dante"])

        network = context["npc_relationship_network"]
        assert network["always_read"] is True
        assert network["directional"] is True
        pairs = {
            (row["owner_character_id"], row["target_character_id"]): row
            for row in network["relations"]
        }
        assert pairs[("adrian", "dante")]["relationship_type"] == "лучшие друзья"
        assert "бесится" in pairs[("adrian", "dante")]["current_dynamic"]
        assert "провоцировать" in pairs[("dante", "adrian")]["current_dynamic"]
        assert pairs[("yuna", "lem")]["relationship_type"] == "бывшие"
        assert pairs[("lem", "yuna")]["relationship_type"] == "бывшие"


def test_selected_offscreen_bundle_contains_npc_relationships():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        bundle = read_bundle(sid, "dante")

        assert bundle["character_id"] == "dante"
        relations = bundle["npc_relationships_director_only"]
        outgoing = next(
            row for row in relations
            if row["owner_character_id"] == "dante"
            and row["target_character_id"] == "adrian"
        )
        assert "провоцировать" in outgoing["current_dynamic"]
        assert "ревность Эдриана" in outgoing["interaction_hooks"]
        assert outgoing["knowledge_scope"] == "director_only_not_personal_knowledge"

        adrian_bundle = read_bundle(sid, "adrian")
        adrian_relations = adrian_bundle["npc_relationships_director_only"]
        assert all(row["owner_character_id"] == "adrian" for row in adrian_relations)
        assert not any(
            row.get("owner_character_id") == "dante"
            and "Эдриан ревнует" in " ".join(row.get("beliefs_about_target", []))
            for row in adrian_relations
        )


def test_runtime_npc_relationship_change_overrides_profile_without_mirroring_reverse():
    cards = novel()["characters"]
    state = {
        "pov": {"character_id": "rina"},
        "npc_relationships": {},
    }

    def resolve(values, raw):
        needle = str(raw or "").casefold()
        for card in values:
            if str(card.get("character_id") or "").casefold() == needle:
                return str(card["character_id"])
            if str(card.get("name") or "").casefold() == needle:
                return str(card["character_id"])
        return None

    changed = npc_relationship_runtime.apply_updates(
        state,
        [
            {
                "owner_character_id": "adrian",
                "target_character_id": "dante",
                "current_dynamic": "после ссоры злится на Данте и избегает его",
                "unresolved_between_them": ["не договорили после провокации"],
                "change_reason": "Данте намеренно довёл его ревностью",
            }
        ],
        cards=cards,
        resolve_character_id=resolve,
        turn_number=12,
    )

    network = npc_relationship_runtime.build_network(
        cards,
        changed,
        resolve_character_id=resolve,
    )
    pairs = {
        (row["owner_character_id"], row["target_character_id"]): row
        for row in network["relations"]
    }
    assert "избегает" in pairs[("adrian", "dante")]["current_dynamic"]
    assert "провоцировать" in pairs[("dante", "adrian")]["current_dynamic"]
    assert pairs[("adrian", "dante")]["last_changed_turn"] == 12
    assert pairs[("dante", "adrian")].get("last_changed_turn") is None


def test_npc_relationship_behavior_lives_in_scene_builder_and_rules_stay_technical():
    rules = Path("runtime/rules.md").read_text(encoding="utf-8")
    builder = Path("runtime/scene_builder.md").read_text(encoding="utf-8")
    instructions = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    schema = Path("openapi.yaml").read_text(encoding="utf-8")

    assert "Не делай POV обязательным центром такого взаимодействия" in builder
    assert "Не превращай естественное взаимодействие NPC↔NPC автоматически в вопрос к POV" in builder
    assert "второстепенная пара" in builder
    assert "npc_relationships_director_only" in rules
    assert "Запись отношения сама по себе знанием не является" in rules
    assert "NPC могут взаимодействовать друг с другом независимо от POV" not in rules
    assert "npc_relationship_updates" in rules
    assert "npc_relationship_updates" in instructions
    assert len(instructions) < 8000
    assert "NPCRelationshipUpdate:" in schema
    assert "npc_relationship_updates:" in schema


def test_legacy_free_text_relation_does_not_guess_between_ambiguous_names():
    cards = [
        {"character_id": "pov", "name": "POV", "is_pov": True},
        {"character_id": "alexey", "name": "Алексей"},
        {"character_id": "alexander", "name": "Александр"},
        {
            "character_id": "owner",
            "name": "Owner",
            "relationships": ["Алекс злится после старого спора"],
        },
    ]
    state = {"pov": {"character_id": "pov"}}

    def resolve(values, raw):
        needle = str(raw or "").casefold()
        for card in values:
            if str(card.get("character_id") or "").casefold() == needle:
                return str(card["character_id"])
            if str(card.get("name") or "").casefold() == needle:
                return str(card["character_id"])
        return None

    network = npc_relationship_runtime.build_network(
        cards,
        state,
        resolve_character_id=resolve,
    )
    assert not any(row["owner_character_id"] == "owner" for row in network["relations"])
