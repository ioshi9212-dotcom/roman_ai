import json
import tempfile
from pathlib import Path

from app import knowledge_slice_runtime, session_runtime, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def base_novel():
    return {
        "novel_id": "knowledge-slice-test",
        "title": "Knowledge Slice Test",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {
            "pov_character": "rina",
            "genres": ["romance"],
            "premise": "Test premise",
            "story_rules": ["Keep continuity"],
        },
        "hidden_lore": {
            "secret": "Director-only truth that must not be in the normal writer packet."
        },
        "characters": [
            {
                "character_id": "rina",
                "name": "Рината",
                "is_pov": True,
                "age": 26,
                "work": "маникюр",
            },
            {
                "character_id": "dante",
                "name": "Дантэ",
                "age": 24,
                "work": "работает с младшей группой",
                "appearance": {"hair": "светло-русые"},
                "remembered_secret": "Он сам это знает.",
                "unknown_origin": {
                    "hidden_from_self": True,
                    "truth": "Скрытая от него авторская правда.",
                },
                "additional": {
                    "normal_self_fact": "любит кофе",
                    "unknown_to_self": {
                        "truth": "ещё одна скрытая ветка",
                    },
                },
            },
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "age": 27,
            },
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "04.04.2026",
                "time": "08:30",
                "location": "Квартира Дантэ",
                "present_characters": ["rina", "dante"],
            },
        },
    }


def read_context(session_id: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(session_id, user_input)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return manifest, json.loads("".join(parts))


def read_all(manifest, session_id: str):
    for index in range(1, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def test_self_known_card_keeps_normal_biography_and_removes_explicit_self_hidden_branches():
    card = base_novel()["characters"][1]
    clean = knowledge_slice_runtime.self_known_card(card)

    assert clean["age"] == 24
    assert clean["work"] == "работает с младшей группой"
    assert clean["appearance"]["hair"] == "светло-русые"
    assert clean["remembered_secret"] == "Он сам это знает."
    assert clean["additional"]["normal_self_fact"] == "любит кофе"
    assert "unknown_origin" not in clean
    assert "unknown_to_self" not in clean["additional"]


def test_active_character_slice_knows_own_age_without_duplicate_memory_entry():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        _, context = read_context(sid, "Сколько тебе лет?")
        dante = context["character_slices"]["dante"]

        assert dante["self_profile"]["age"] == 24
        assert dante["self_profile"]["work"] == "работает с младшей группой"
        assert dante["personal_memory"]["knowledge_journal"] == []
        assert dante["knowledge_scope"]["ordinary_self_facts_do_not_need_duplicate_memory"] is True


def test_writer_packet_uses_slices_instead_of_raw_director_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        _, context = read_context(sid, "(посмотреть на Дантэ)")

        assert context["knowledge_architecture"]["mode"] == "scene_local_character_slices_v1"
        assert context["knowledge_architecture"]["hard_claim_gate"] is False
        assert context["knowledge_architecture"]["raw_chronology_in_writer_packet"] is False
        assert context["knowledge_architecture"]["raw_recent_turns_in_writer_packet"] is False
        assert context["knowledge_architecture"]["raw_hidden_lore_in_writer_packet"] is False

        for raw_key in (
            "recent_turns",
            "continuity_turns",
            "scene_history",
            "chronology_recent",
            "chronology_anchor_catalog",
            "character_cards",
            "character_profiles",
            "character_memory",
            "hidden_lore",
            "author_context",
        ):
            assert raw_key not in context

        assert set(context["character_slices"]) == {"rina", "dante"}
        assert "adrian" not in context["character_slices"]
        assert context["director_cues"]["novel_direction"]["premise"] == "Test premise"
        assert "hidden_lore" not in context["director_cues"]


def test_private_photo_event_is_not_replayed_as_raw_history_in_next_writer_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(сфотографировать спящего Дантэ и отправить Эдриану - вот фото)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(сфотографировать спящего Дантэ и отправить Эдриану - вот фото)",
                "scene_output": (
                    "Рината сфотографировала спящего Дантэ и отправила фото Эдриану.\n"
                    "Дантэ продолжал спать."
                ),
                "extracted": {},
            },
        )

        _, context = read_context(sid, "(допить кофе)")
        blob = json.dumps(context, ensure_ascii=False)

        assert "Рината сфотографировала спящего Дантэ и отправила фото Эдриану" not in blob
        assert context["scene_contract"]["public_dialogue_continuity"] == []
        assert context["character_slices"]["dante"]["personal_memory"]["knowledge_journal"] == []


def test_current_private_communication_has_explicit_recipient_scope():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        _, context = read_context(
            sid,
            "(ответить Эдриану - завтра вернусь. продолжать пить кофе)",
        )
        scopes = context["scene_contract"]["current_private_communications"]

        assert len(scopes) == 1
        assert scopes[0]["recipient_id"] == "adrian"
        assert set(scopes[0]["visible_to_character_ids"]) == {"rina", "adrian"}
        assert "dante" in scopes[0]["not_visible_to_character_ids"]
