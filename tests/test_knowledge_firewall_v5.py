import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import knowledge_firewall_runtime as firewall
from app import operation_service, storage


def _setup_temp_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel(*, emily_knows_age: bool = False, silas_knows_age: bool = False):
    emily_knowledge = [
        {"fact_id": "emily_knows_silas_name", "fact": "Мужчину зовут Сайлас."},
    ]
    if emily_knows_age:
        emily_knowledge.append({"fact_id": "emily_knows_silas_age", "fact": "Сайласу 400 лет."})

    silas_knowledge = []
    if silas_knows_age:
        silas_knowledge.append({"fact_id": "silas_knows_own_age", "fact": "Сайласу 400 лет."})

    return {
        "novel_id": "strict_knowledge_400",
        "title": "Strict Knowledge",
        "novel": {"pov_character": "emily"},
        "characters": [
            {"character_id": "emily", "name": "Эмили", "is_pov": True},
            {
                "character_id": "silas",
                "name": "Сайлас",
                "role": "major",
                "age": 400,
                "backstory": {"secret": "Сайлас прожил четыреста лет."},
            },
        ],
        "lore": {"observer_age": 400},
        "foundation": {
            "facts": [
                {
                    "fact_id": "author_silas_age",
                    "text": "Сайласу 400 лет.",
                    "stored_in": ["characters.silas.age"],
                    "story_use": "reference",
                }
            ],
            "hooks": [],
            "story_pillars": [{"pillar_id": "silas_secret", "label": "Тайна Сайласа", "source_fact_ids": ["author_silas_age"]}],
        },
        "knowledge": {
            "characters": {
                "emily": {"knowledge": emily_knowledge},
                "silas": {"knowledge": silas_knowledge},
            }
        },
        "starting_state": {
            "pov": {"character_id": "emily"},
            "current": {
                "date": "2026-09-22",
                "time": "01:37",
                "location": "комната",
                "present_characters": ["emily", "silas"],
            },
        },
    }


def _prepare_strict_turn(session_id: str, user_input: str = "(посмотреть на Сайласа)"):
    packet = operation_service.prepare_turn_request(
        session_id,
        user_input,
        request_id="strict-request",
        scene_archive_capable=False,
        knowledge_review_capable=True,
        strict_knowledge_capable=True,
        replace_pending=False,
    )
    for index in packet.get("pending_turn", {}).get("unread_chunk_indices", []):
        storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)
    return packet


def _payload(scene_output: str, *, usage, turn_knowledge=None):
    return {
        "packet_id": "unused-by-direct-validator",
        "user_input": "(посмотреть на Сайласа)",
        "scene_output": scene_output,
        "extracted": {
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "knowledge_trace_complete": True,
            "turn_knowledge": turn_knowledge or [],
            "knowledge_usage": usage,
            "chronology": [],
            "knowledge_add": [],
            "experiences_add": [],
            "dialogue_memory_add": [],
            "npc_intent_updates": [],
            "story_thread_updates": [],
        },
    }


def test_initial_knowledge_is_seeded_only_from_dedicated_knowledge_section():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]

        emily = storage.get_character_memory(sid, "emily")
        facts = {row["fact_id"]: row["fact"] for row in emily["knowledge"]}

        assert facts == {"emily_knows_silas_name": "Мужчину зовут Сайлас."}
        assert all("400" not in text for text in facts.values())


def test_writer_packet_frontloads_closed_world_knowledge_and_strips_fact_authority_from_recollection():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        packet = _prepare_strict_turn(sid)

        chunks = [packet["content"]]
        for index in range(1, packet["chunk_count"]):
            chunks.append(storage.get_turn_packet_chunk(sid, packet["packet_id"], index)["content"])
        context = json.loads("".join(chunks))

        assert next(iter(context)) == "knowledge_firewall_v5"
        assert context["knowledge_firewall_v5"]["closed_world"] is True
        assert context["knowledge_firewall_v5"]["version"] == 10
        assert context["character_memory"]["emily"]["knowledge"][0]["fact_id"] == "emily_knows_silas_name"
        assert "character_knowledge" not in context
        assert context["dialogue_frames"]["emily"]["knowledge_path"] == "character_memory[emily].knowledge"
        assert context["dialogue_frames"]["emily"]["self_card_path"] == "character_cards[character_id=emily].card"
        assert "character_drivers" not in context["dialogue_frames"]["emily"]
        assert "relationship" not in context["dialogue_frames"]["emily"]
        assert "active_intents" not in context["dialogue_frames"]["emily"]
        assert context["dialogue_frames"]["emily"]["actor_frame_path"] is None
        assert context["dialogue_policy"]["scope"] == "real_speech_only"
        assert "experiences" not in context["character_memory"]["emily"]
        assert context["author_only_recollection_context"]["emily"]["fact_authority"] is False


def test_silas_can_use_own_age_from_his_card_without_duplicate_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare_strict_turn(sid)

        payload = _payload(
            "**Сайлас** — Мне 400 лет.",
            usage=[{
                "unit_id": "speech:1",
                "character_id": "silas",
                "speech_text": "Мне 400 лет.",
                "claims_reviewed": True,
                "claims": [{
                    "claim": "Сайласу 400 лет.",
                    "source_self_paths": ["age"],
                }],
            }],
        )

        firewall._validate_knowledge_commit(sid, payload)


def test_self_card_path_marked_hidden_from_self_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        novel = _novel()
        silas = next(row for row in novel["characters"] if row["character_id"] == "silas")
        silas["sealed_truth"] = {"hidden_from_self": True, "age": 777}
        sid = storage.create_session(novel)["session_id"]
        _prepare_strict_turn(sid)

        payload = _payload(
            "**Сайлас** — Мне 777 лет.",
            usage=[{
                "unit_id": "speech:1",
                "character_id": "silas",
                "speech_text": "Мне 777 лет.",
                "claims_reviewed": True,
                "claims": [{
                    "claim": "Сайласу 777 лет.",
                    "source_self_paths": ["sealed_truth.age"],
                }],
            }],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)
        assert exc.value.detail["code"] == "KNOWLEDGE_SELF_SOURCE_INVALID"


def test_canon_fill_creates_undefined_self_detail_and_is_persisted_once():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare_strict_turn(sid)

        event = {
            "event_id": "silas_workplace_fill",
            "character_id": "silas",
            "subject_character_id": "silas",
            "fact": "Сайлас работает реставратором.",
            "source_kind": "canon_fill",
            "canon_gap": True,
            "gap_kind": "self_detail",
            "detail_key": "workplace",
        }
        payload = _payload(
            "**Сайлас** — Я работаю реставратором.",
            usage=[{
                "unit_id": "speech:1",
                "character_id": "silas",
                "speech_text": "Я работаю реставратором.",
                "claims_reviewed": True,
                "claims": [{
                    "claim": "Сайлас работает реставратором.",
                    "source_event_ids": ["silas_workplace_fill"],
                }],
            }],
            turn_knowledge=[event],
        )

        turn_events = firewall._validate_knowledge_commit(sid, payload)
        firewall._augment_canon_fill_persistence(payload, turn_events)

        knowledge_add = payload["extracted"]["knowledge_add"]
        assert len(knowledge_add) == 1
        assert knowledge_add[0]["source_event_id"] == "silas_workplace_fill"
        assert knowledge_add[0]["generated_detail_key"]
        upsert = next(row for row in payload["extracted"]["character_upserts"] if row["character_id"] == "silas")
        assert upsert["generated_details"]

        root = storage.SESSIONS_DIR / sid
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        cards = storage._apply_character_upserts(cards, payload["extracted"])
        storage._write_json(root / "characters.json", cards)

        with pytest.raises(HTTPException) as exc:
            firewall._validate_turn_knowledge(
                {"turn_knowledge": [event]},
                root=root,
                user_input="",
                scene_output="",
                valid_character_ids={"emily", "silas"},
            )
        assert exc.value.detail["code"] == "TURN_KNOWLEDGE_CANON_FILL_NOT_A_GAP"


def test_pov_cannot_use_age_from_silas_card_foundation_or_lore_as_fact_free_dialogue():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare_strict_turn(sid)

        payload = _payload(
            "**Эмили** — Тебе четыреста лет.",
            usage=[{
                "unit_id": "speech:1",
                "character_id": "emily",
                "speech_text": "Тебе четыреста лет.",
                "claims_reviewed": True,
                "claims": [],
            }],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_LITERAL_LEAK"
        assert exc.value.detail["character_id"] == "emily"
        assert exc.value.detail["literal"] == 400


def test_card_fact_id_cannot_be_forged_as_pov_knowledge_source():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel())["session_id"]
        _prepare_strict_turn(sid)

        payload = _payload(
            "**Эмили** — Тебе 400 лет.",
            usage=[
                {
                    "unit_id": "speech:1",
                    "character_id": "emily",
                    "speech_text": "Тебе 400 лет.",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Сайласу 400 лет.",
                        "source_fact_ids": ["author_silas_age"],
                    }],
                }
            ],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_USAGE_UNKNOWN_FACT"
        assert exc.value.detail["unknown_fact_ids"] == ["author_silas_age"]


def test_current_turn_fact_is_usable_only_after_real_evidence_precedes_use():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel(silas_knows_age=True))["session_id"]
        _prepare_strict_turn(sid)

        scene = "**Сайлас** — Мне четыреста лет.\n**Эмили** — Тебе четыреста лет?"
        payload = _payload(
            scene,
            usage=[
                {
                    "unit_id": "speech:1",
                    "character_id": "silas",
                    "speech_text": "Мне четыреста лет.",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Сайласу 400 лет.",
                        "source_fact_ids": ["silas_knows_own_age"],
                    }],
                },
                {
                    "unit_id": "speech:2",
                    "character_id": "emily",
                    "speech_text": "Тебе четыреста лет?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Сайласу 400 лет.",
                        "source_event_ids": ["emily_hears_age"],
                    }],
                },
            ],
            turn_knowledge=[
                {
                    "event_id": "emily_hears_age",
                    "character_id": "emily",
                    "fact": "Сайлас сказал, что ему 400 лет.",
                    "source_kind": "told",
                    "evidence": "Мне четыреста лет.",
                }
            ],
        )

        firewall._validate_knowledge_commit(sid, payload)


def test_retroactive_source_after_pov_line_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        _setup_temp_storage(tmp)
        sid = storage.create_session(_novel(silas_knows_age=True))["session_id"]
        _prepare_strict_turn(sid)

        scene = "**Эмили** — Тебе четыреста лет?\n**Сайлас** — Мне четыреста лет."
        payload = _payload(
            scene,
            usage=[
                {
                    "unit_id": "speech:1",
                    "character_id": "emily",
                    "speech_text": "Тебе четыреста лет?",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Сайласу 400 лет.",
                        "source_event_ids": ["emily_hears_age"],
                    }],
                },
                {
                    "unit_id": "speech:2",
                    "character_id": "silas",
                    "speech_text": "Мне четыреста лет.",
                    "claims_reviewed": True,
                    "claims": [{
                        "claim": "Сайласу 400 лет.",
                        "source_fact_ids": ["silas_knows_own_age"],
                    }],
                },
            ],
            turn_knowledge=[
                {
                    "event_id": "emily_hears_age",
                    "character_id": "emily",
                    "fact": "Сайлас сказал, что ему 400 лет.",
                    "source_kind": "told",
                    "evidence": "Мне четыреста лет.",
                }
            ],
        )

        with pytest.raises(HTTPException) as exc:
            firewall._validate_knowledge_commit(sid, payload)

        assert exc.value.detail["code"] == "KNOWLEDGE_SOURCE_AFTER_USE"
