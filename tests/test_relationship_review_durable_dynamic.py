import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import relationship_file_runtime, storage, turn_pipeline
from app.operation_service import commit_turn_request, prepare_turn_request


def setup(tmp, dims=None, dynamic=""):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()
    relation = {"target_character_id": "kair", "dimensions": dims or []}
    if dynamic:
        relation["current_dynamic"] = dynamic
    novel = {
        "novel_id": "rel-review", "title": "Rel Review", "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "kair"},
        "characters": [
            {"character_id": "kair", "name": "Кайр", "is_pov": True},
            {"character_id": "mira", "name": "Мира", "relationships": [relation]},
        ],
        "starting_state": {"pov": {"character_id": "kair"}, "current": {
            "date": "07.10.2026", "time": "23:00", "location": "flat",
            "present_characters": ["kair", "mira"],
        }},
    }
    return storage.create_session(novel)["session_id"]


def prepare(sid):
    manifest = prepare_turn_request(sid, "(молчать)", request_id="rel-review")
    for index in range(1, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    return manifest


def payload(manifest, updates=None, result="no_numeric_dimension_justified", changed=False):
    return {
        "packet_id": manifest["packet_id"], "user_input": "(молчать)",
        "scene_output": "**Мира** — Я тебя услышала.",
        "extracted": {
            "scene_builder_reviewed": True, "persistence_reviewed": True,
            "knowledge_reviewed": True, "relationship_updates": updates or [],
            "relationship_review": [{
                "character_id": "mira", "changed": changed,
                "reason": "Проверено устойчивое отношение Миры к Кайру.",
                "numeric_result": result,
            }],
        },
    }


def store(sid):
    return storage._read_json(storage.SESSIONS_DIR / sid / "relationships.json", {})


def test_public_turn_requires_review_and_empty_durable_dynamic_cannot_pass_silently():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp, dynamic="Мира устойчива насторожена к Кайру.")
        manifest = prepare(sid)
        assert manifest["relationship_review_required"] is True
        data = payload(manifest)
        data["extracted"].pop("relationship_review")
        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)
        assert exc.value.detail["code"] == "RELATIONSHIP_DIMENSIONS_EMPTY_WITH_DURABLE_DYNAMIC"


def test_stale_pending_packet_rebuild_keeps_public_review_marker():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp)
        first = prepare(sid)
        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        packet["turn_pipeline_version"] = 19
        packet.pop("relationship_review_required", None)
        storage._write_json(root / "turn_packet.json", packet)

        second = prepare_turn_request(sid, "(молчать)", request_id="rel-review")
        rebuilt = storage._read_json(root / "turn_packet.json", {})

        assert second["packet_id"] != first["packet_id"]
        assert second["relationship_review_required"] is True
        assert second["writer_review_required"] is True
        assert rebuilt["turn_pipeline_version"] == 20
        assert rebuilt["writer_review_required"] is True
        assert rebuilt["relationship_review_required"] is True


def test_empty_durable_dynamic_cannot_be_waived_with_no_numeric_result():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp, dynamic="Мира пока насторожена к Кайру.")
        manifest = prepare(sid)
        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, payload(manifest))
        assert exc.value.detail["code"] == "RELATIONSHIP_DIMENSIONS_EMPTY_WITH_DURABLE_DYNAMIC"
        assert "настороженность" in exc.value.detail["evidenced_dimensions"]


def test_uncertain_qualitative_dynamic_can_explicitly_have_no_numeric_axis():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(
            tmp,
            dynamic="Мира пока присматривается к Кайру и не решила, доверяет ли ему.",
        )
        manifest = prepare(sid)
        data = payload(manifest)
        data["extracted"]["relationship_review"][0]["reason"] = (
            "Проверено: пока нет устойчивого числового отношения."
        )
        assert commit_turn_request(sid, data)["turn_number"] == 1
        assert store(sid)["npc_to_pov"]["mira"]["dimensions"] == {}


def test_mid_scene_physical_entrant_is_in_review_scope_even_if_absent_at_prepare():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp)
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["current"]["present_characters"] = ["kair"]
        storage._write_json(root / "state.json", state)

        manifest = prepare(sid)
        packet = storage._read_json(root / "turn_packet.json", {})
        context = json.loads("".join(packet["chunks"]))
        lens = context["relationship_lens"]
        assert "mira" not in lens["review_required_character_ids_at_scene_start"]
        assert "not exhaustive" in lens["review_scope"]

        data = payload(manifest)
        data["scene_output"] = "**Мира** — Я зашла ненадолго."
        data["extracted"]["presence_updates"] = [
            {"character_id": "mira", "action": "enter"}
        ]
        data["extracted"]["relationship_review"][0]["reason"] = (
            "Мира вошла в сцену; устойчивого числового отношения пока не проявилось."
        )
        assert commit_turn_request(sid, data)["turn_number"] == 1


def test_historical_new_dimension_value_does_not_become_critical_event():
    n = {
        "novel_id": "replay-scale",
        "title": "Replay Scale",
        "novel": {"pov_character": "kair"},
        "characters": [
            {"character_id": "kair", "name": "Кайр", "is_pov": True},
            {"character_id": "mira", "name": "Мира"},
        ],
        "starting_state": {"pov": {"character_id": "kair"}},
    }
    cards = n["characters"]
    base = relationship_file_runtime.build_initial_store(cards, n["starting_state"], "kair")
    replay = relationship_file_runtime._historical_updates_for_replay(
        base,
        [{
            "character_id": "mira",
            "reason": "Исторически уже сформировалась выраженная настороженность.",
            "dimensions": [{"label": "настороженность", "value": 50}],
        }],
        cards=cards,
    )
    assert replay[0]["dimensions"] == [{"label": "настороженность", "value": 50}]
    assert replay[0]["change_scale"] == "ordinary"


def test_new_dimension_initializes_above_three_without_critical_event():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp)
        manifest = prepare(sid)
        update = [{
            "character_id": "mira",
            "reason": "Повторные события сформировали заметную настороженность.",
            "dimensions": [{"label": "настороженность", "value": 35}],
        }]
        commit_turn_request(sid, payload(manifest, update, "updated", True))
        assert store(sid)["npc_to_pov"]["mira"]["dimensions"]["настороженность"]["value"] == 35


def test_last_trust_axis_can_be_replaced_by_evidenced_caution_same_turn():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp, [{"label": "доверие", "value": 2}], "Мира сомневается в Кайре.")
        manifest = prepare(sid)
        update = [{
            "character_id": "mira",
            "reason": "Доверие исчерпано, но закрепилась выраженная настороженность.",
            "dimensions": [
                {"label": "доверие", "delta": -2},
                {"label": "настороженность", "value": 50},
            ],
            "dynamic": "Насторожена к Кайру и не доверяет его мотивам.",
        }]
        commit_turn_request(sid, payload(manifest, update, "updated", True))
        dims = store(sid)["npc_to_pov"]["mira"]["dimensions"]
        assert "доверие" not in dims
        assert dims["настороженность"]["value"] == 50


def test_dynamic_only_change_keeps_numeric_result_unchanged_and_review_is_not_persisted():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp, [{"label": "настороженность", "value": 45}], "Мира подозревает Кайра.")
        manifest = prepare(sid)
        update = [{
            "character_id": "mira",
            "reason": "Прямые ответы немного изменили трактовку его мотивов.",
            "dynamic": "Мира всё ещё насторожена, но меньше подозревает сговор с Адрианом.",
        }]
        commit_turn_request(sid, payload(manifest, update, "unchanged", True))
        rel = store(sid)["npc_to_pov"]["mira"]
        assert rel["dimensions"]["настороженность"]["value"] == 45
        assert "меньше подозревает" in rel["dynamic"]
        turn = storage._read_turns(storage.SESSIONS_DIR / sid)[-1]
        assert "relationship_review" not in turn.get("extracted", {})


def test_historical_persistence_repair_initializes_caution_on_original_turn():
    with tempfile.TemporaryDirectory() as tmp:
        sid = setup(tmp)
        root = storage.SESSIONS_DIR / sid

        relationships = relationship_file_runtime.normalize_store(
            storage._read_json(root / "relationships.json", {}),
            "kair",
        )
        relationships.setdefault("npc_to_pov", {})["mira"] = {
            "dimensions": {},
            "dynamic": (
                "Насторожена к Кайру и не доверяет ему; прямые ответы немного "
                "снизили подозрение, что он изначально действовал вместе с Адрианом."
            ),
        }
        storage._write_json(root / "relationships.json", relationships)

        turns = [
            {
                "turn_number": number,
                "user_input": f"Ход {number}.",
                "scene_output": (
                    "Мира сталкивается с очередным доказанным основанием насторожиться к Кайру."
                    if number == 6
                    else f"Сохранённый ход {number}."
                ),
                "extracted": {},
            }
            for number in range(1, 16)
        ]
        (root / "turns.jsonl").write_text(
            "".join(__import__("json").dumps(row, ensure_ascii=False) + "\n" for row in turns),
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 15
        meta["last_audit_turn"] = 0
        meta["audit_required"] = True
        storage._write_json(root / "meta.json", meta)

        result = turn_pipeline.commit_audit(
            sid,
            {
                "audit_id": "mira-persistence-repair",
                "start_turn": 1,
                "end_turn": 15,
                "repairs": {
                    "relationship_updates": [
                        {
                            "turn": 6,
                            "character_id": "mira",
                            "reason": (
                                "Уже сыгранные сцены закрепили выраженную настороженность; "
                                "раньше numeric dimension не была сохранена."
                            ),
                            "dimensions": [
                                {"label": "настороженность", "value": 50}
                            ],
                            "dynamic": (
                                "Насторожена к Кайру и не доверяет ему; прямые ответы немного "
                                "снизили подозрение, что он изначально действовал вместе с Адрианом."
                            ),
                        }
                    ],
                    "scene_compactions": [
                        {
                            "start_turn": 1,
                            "end_turn": 15,
                            "summary": (
                                "Пятнадцать проверенных ходов сохранены; на шестом ходе уже "
                                "доказанно закрепилась настороженность Миры к Кайру."
                            ),
                            "status": "open",
                            "participants": ["kair", "mira"],
                            "location": "flat",
                        }
                    ],
                },
                "notes": [],
            },
        )

        assert result["audited_through"] == 15
        relation = storage._read_json(root / "relationships.json", {})["npc_to_pov"]["mira"]
        caution = relation["dimensions"]["настороженность"]
        assert caution["value"] == 50
        assert caution["last_change"]["turn"] == 6
        assert "насторожена" in relation["dynamic"].casefold()
        assert relation["dynamic_last_change"]["turn"] == 6
