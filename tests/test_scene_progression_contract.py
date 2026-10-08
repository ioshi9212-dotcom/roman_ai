import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import storage
from app.operation_service import commit_turn_request, prepare_turn_request


def _setup(tmp: str, *, with_thread: bool = False, with_mira: bool = False, with_offscreen: bool = False) -> str:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()

    characters = [{"character_id": "kair", "name": "Кайр", "is_pov": True}]
    present = ["kair"]
    if with_offscreen:
        characters.append({"character_id": "daren", "name": "Дарен", "story_function": "Ищет повреждённый переход"})
    if with_mira:
        characters.append({
            "character_id": "mira",
            "name": "Мира",
            "relationships": [{
                "target_character_id": "kair",
                "dimensions": [{"label": "настороженность", "value": 45}],
                "current_dynamic": "Мира устойчива насторожена к Кайру.",
            }],
        })
        present.append("mira")

    starting_state = {
        "pov": {"character_id": "kair"},
        "current": {
            "date": "08.10.2026",
            "time": "04:26",
            "location": "дом Кайра",
            "present_characters": present,
            "unfinished_actions": [],
        },
    }
    if with_thread:
        starting_state["threads"] = {
            "chronometer": {
                "thread_id": "chronometer",
                "title": "Хронометр",
                "summary": "Понять, почему хронометр реагирует на девятиминутные интервалы.",
                "status": "active",
                "priority": "high",
                "current_goal": "Проверить следующее объективное последствие аномалии.",
                "last_progress_turn": 0,
            }
        }

    novel = {
        "novel_id": "progression-contract",
        "title": "Progression Contract",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "kair"},
        "characters": characters,
        "starting_state": starting_state,
    }
    return storage.create_session(novel)["session_id"]


def _packet_context(sid: str, manifest: dict) -> dict:
    root = storage.SESSIONS_DIR / sid
    packet = storage._read_json(root / "turn_packet.json", {})
    return json.loads("".join(packet["chunks"]))


def _prepare(sid: str, user_input: str) -> tuple[dict, dict]:
    manifest = prepare_turn_request(sid, user_input, request_id="progression-test")
    for index in range(1, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
    return manifest, _packet_context(sid, manifest)


def _payload(
    manifest: dict,
    user_input: str,
    scene_output: str,
    *,
    scene_progressed: bool,
    proof: dict | None = None,
    chronology=None,
    thread_updates=None,
    relationship_updates=None,
    relationship_review=None,
    presence_updates=None,
):
    extracted = {
        "scene_builder_reviewed": True,
        "persistence_reviewed": True,
        "knowledge_reviewed": True,
        "chronology": chronology or [],
        "knowledge_journal_add": [],
        "npc_intent_updates": [],
        "npc_relationship_updates": [],
        "story_thread_updates": thread_updates or [],
        "scene_progressed": scene_progressed,
        "presence_updates": presence_updates or [],
        "relationship_updates": relationship_updates or [],
        "relationship_review": relationship_review or [],
        "state_patch": {},
        "character_upserts": [],
    }
    if proof is not None:
        extracted["scene_progression"] = proof
    return {
        "packet_id": manifest["packet_id"],
        "user_input": user_input,
        "scene_output": scene_output,
        "extracted": extracted,
    }


def _thread_proof(ending_text: str) -> dict:
    return {
        "target": "thread:chronometer",
        "kind": "thread_state_change",
        "action": "Аномалия хронометра дала новый наблюдаемый результат.",
        "end_state_change": "После пробуждения у Кайра есть новая отметка, которой не было до сна.",
        "ending_kind": "concrete_next_pressure",
        "ending_evidence_text": ending_text,
    }


def _thread_update() -> list[dict]:
    return [{
        "thread_id": "chronometer",
        "operation": "upsert",
        "progressed_now": True,
        "progress_summary": "Хронометр после сна показал новую аномальную отметку.",
    }]


def test_prepare_packet_exposes_target_driven_time_skip_contract():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        manifest, context = _prepare(sid, "(спать до пробуждения)")

        assert manifest["progression_review_required"] is True
        contract = context["progression_contract"]
        assert contract["mandatory"] is True
        assert contract["time_skip_requested"] is True
        assert contract["explicit_uneventful_downtime"] is False
        ids = {row["target_id"] for row in contract["eligible_targets"]}
        assert "thread:chronometer" in ids
        assert "world:emergent" in ids
        assert contract["proof_required_in_commit"] is True


def test_sleep_and_time_passage_alone_are_rejected_even_with_scene_progressed_true():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        raw = "(спать до пробуждения)"
        manifest, _ = _prepare(sid, raw)
        data = _payload(
            manifest,
            raw,
            "Кайр проверил телефон, лёг спать и проснулся в 10:41. Всё было тихо.",
            scene_progressed=True,
        )

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)

        assert exc.value.detail["code"] == "SCENE_NO_MEANINGFUL_PROGRESSION"
        assert exc.value.detail["time_skip_requested"] is True


def test_explicit_uneventful_downtime_can_remain_uneventful():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        raw = "(спать до утра без событий)"
        manifest, context = _prepare(sid, raw)
        assert context["progression_contract"]["explicit_uneventful_downtime"] is True

        result = commit_turn_request(
            sid,
            _payload(
                manifest,
                raw,
                "Кайр лёг спать. Утро пришло без событий.",
                scene_progressed=False,
            ),
        )
        assert result["turn_number"] == 1


def test_thread_progression_with_concrete_final_consequence_commits_and_proof_is_transient():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        raw = "(спать до пробуждения)"
        manifest, _ = _prepare(sid, raw)
        ending = "На дисплее впервые появилась новая отметка."
        data = _payload(
            manifest,
            raw,
            "Ночь прошла рваным сном. Кайр проснулся от короткого сигнала хронометра. "
            + ending,
            scene_progressed=True,
            proof=_thread_proof(ending),
            thread_updates=_thread_update(),
        )

        result = commit_turn_request(sid, data)
        assert result["turn_number"] == 1
        saved = storage._read_turns(storage.SESSIONS_DIR / sid)[-1]
        assert "scene_progression" not in saved["extracted"]
        thread = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})["threads"]["chronometer"]
        assert thread["last_progress_turn"] == 1


def test_meaningful_change_with_passive_ending_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        raw = "(спать до пробуждения)"
        manifest, _ = _prepare(sid, raw)
        ending = "Утром всё было тихо."
        data = _payload(
            manifest,
            raw,
            "Перед сном хронометр показал новую отметку, и Кайр записал её. "
            "Потом он лёг спать. " + ending,
            scene_progressed=True,
            proof=_thread_proof(ending),
            thread_updates=_thread_update(),
        )

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)

        assert exc.value.detail["code"] == "SCENE_PASSIVE_ENDING"


def test_fake_ominous_sentence_cannot_replace_a_real_ending_hook():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        raw = "(заняться обычными делами до вечера)"
        manifest, _ = _prepare(sid, raw)
        ending = "Кайр ещё не знал, что этот день изменит всё."
        data = _payload(
            manifest,
            raw,
            "Хронометр утром показал новую отметку. " + ending,
            scene_progressed=True,
            proof=_thread_proof(ending),
            thread_updates=_thread_update(),
        )

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)

        assert exc.value.detail["code"] == "SCENE_PASSIVE_ENDING"


def test_relationship_line_can_be_the_meaningful_progression_target():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_mira=True)
        raw = "Остаться рядом с Мирой."
        manifest, context = _prepare(sid, raw)
        ids = {row["target_id"] for row in context["progression_contract"]["eligible_targets"]}
        assert "relationship:mira" in ids
        assert "relationship:kair" not in ids
        assert "world:emergent" in ids

        ending = "Мира впервые сама задержала его ладонь в своей."
        proof = {
            "target": "relationship:mira",
            "kind": "relationship_shift",
            "action": "Мира сознательно сократила дистанцию и немного снизила настороженность.",
            "end_state_change": "К концу сцены она впервые сама удерживает физический контакт.",
            "ending_kind": "relationship_shift",
            "ending_evidence_text": ending,
        }
        update = [{
            "character_id": "mira",
            "reason": "Мира сама удержала контакт после спокойного разговора.",
            "dimensions": [{"label": "настороженность", "delta": -1}],
            "dynamic": "Всё ещё насторожена, но впервые сама удержала близкий физический контакт.",
        }]
        review = [{
            "character_id": "mira",
            "changed": True,
            "reason": "Настороженность немного снизилась после её собственной инициативы.",
            "numeric_result": "updated",
        }]
        data = _payload(
            manifest,
            raw,
            "Разговор не сделал Миру доверчивой, но дистанция стала другой. " + ending,
            scene_progressed=True,
            proof=proof,
            relationship_updates=update,
            relationship_review=review,
        )

        result = commit_turn_request(sid, data)
        assert result["turn_number"] == 1
        rel = storage._read_json(storage.SESSIONS_DIR / sid / "relationships.json", {})["npc_to_pov"]["mira"]
        assert rel["dimensions"]["настороженность"]["value"] == 44


def test_desire_not_to_be_disturbed_is_not_explicit_uneventful_downtime():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        _, context = _prepare(sid, "(лечь спать, чтобы никто не мешал)")

        contract = context["progression_contract"]
        assert contract["time_skip_requested"] is True
        assert contract["explicit_uneventful_downtime"] is False
        assert contract["required_target_count"] == 1


def test_normal_chronology_without_consequence_cannot_fake_progression():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp)
        raw = "(осмотреть комнату)"
        manifest, _ = _prepare(sid, raw)
        ending = "Кайр заметил, что чашка стоит на столе."
        data = _payload(
            manifest,
            raw,
            ending,
            scene_progressed=True,
            proof={
                "target": "world:emergent",
                "kind": "new_information",
                "action": ending,
                "end_state_change": "Кайр увидел обычную чашку.",
                "ending_kind": "new_fact",
                "ending_evidence_text": ending,
            },
            chronology=[{
                "event": ending,
                "importance": "normal",
            }],
        )

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)

        assert exc.value.detail["code"] == "SCENE_NO_MEANINGFUL_PROGRESSION"


def test_continuing_existing_remote_chat_is_not_progression_by_itself():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_mira=True)
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        state["current"]["present_characters"] = ["kair"]
        state["current"]["remote_characters"] = ["mira"]
        state["current"]["remote_channels"] = {"mira": "messages"}
        storage._write_json(root / "state.json", state)

        raw = "Ответить Мире."
        manifest, _ = _prepare(sid, raw)
        ending = "**Мира** (сообщение) — Привет."
        data = _payload(
            manifest,
            raw,
            ending,
            scene_progressed=True,
            proof={
                "target": "world:emergent",
                "kind": "npc_action",
                "action": "Мира продолжила уже идущую переписку.",
                "end_state_change": "В текущем чате появилась ещё одна обычная реплика.",
                "ending_kind": "incoming_contact",
                "ending_evidence_text": ending,
            },
        )

        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, data)

        assert exc.value.detail["code"] == "SCENE_NO_MEANINGFUL_PROGRESSION"


def test_spoken_wish_for_a_quiet_night_does_not_disable_progression():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_thread=True)
        _, context = _prepare(sid, "Надеюсь, ночь пройдёт без событий.")

        contract = context["progression_contract"]
        assert contract["time_skip_requested"] is False
        assert contract["explicit_uneventful_downtime"] is False
        assert contract["required_target_count"] == 1


def test_independent_cast_goals_are_progression_targets_without_intent():
    from app.scene_progression_runtime import _cast_candidates, build_contract
    context = {
        "cast_registry": {
            "characters": [
                {"character_id": "daren", "offscreen_can_initiate": True},
                {"character_id": "var", "offscreen_can_initiate": True},
            ]
        },
        "scene_presence": {"present_character_ids": ["kair"], "remote_character_ids": []},
        "player_input_map": {"stage_directions": []},
    }
    state = {"pov": {"character_id": "kair"}, "current": {}}
    targets = build_contract(
        state=state, context=context, user_input="(заняться делами)",
        cards=[{"character_id": "kair"}], current_turn=6,
    )["eligible_targets"]
    ids = {row["target_id"] for row in targets}
    assert {"cast:independent", "world:emergent"} <= ids
    assert not state.get("npc_intents")


def test_offscreen_npc_can_appear_without_pov_prompt_or_preexisting_intent():
    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_offscreen=True)
        raw = "(читать книгу)"
        manifest, context = _prepare(sid, raw)
        ids = {row["target_id"] for row in context["progression_contract"]["eligible_targets"]}
        assert "cast:independent" in ids
        assert not storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {}).get("npc_intents")

        ending = "Дарен появился в дверях и спросил о повреждённом переходе."
        data = _payload(
            manifest, raw, "Кайр читал книгу. " + ending,
            scene_progressed=True,
            proof={
                "target": "cast:independent",
                "character_id": "daren",
                "kind": "npc_action",
                "action": "Дарен пришёл по собственному расследованию.",
                "end_state_change": "Дарен физически вошёл в комнату и задал вопрос.",
                "ending_kind": "incoming_contact",
                "ending_evidence_text": ending,
            },
            presence_updates=[{"character_id": "daren", "action": "enter"}],
        )
        # New physical participants require a relationship review even if no
        # numeric dimension changed; the character's entrance is not a POV command.
        data["extracted"]["relationship_review"] = [{
            "character_id": "daren", "changed": False,
            "reason": "Первое появление без устойчивого сдвига отношений.",
            "numeric_result": "no_numeric_dimension_justified",
        }]
        result = commit_turn_request(sid, data)
        assert result["turn_number"] == 1


def test_http_commit_preserves_independent_cast_actor_through_pydantic():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import CommitTurnRequest

    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_offscreen=True)
        raw = "(читать книгу)"
        manifest, _ = _prepare(sid, raw)
        ending = "Дарен вошёл и сообщил об обнаруженном повреждении перехода."
        data = _payload(
            manifest,
            raw,
            "Кайр читал книгу. " + ending,
            scene_progressed=True,
            proof={
                "target": "cast:independent",
                "character_id": "daren",
                "kind": "npc_action",
                "action": "Дарен самостоятельно пришёл из-за расследования.",
                "end_state_change": "Дарен встретился с Кайром, чтобы сообщить о повреждении.",
                "ending_kind": "incoming_contact",
                "ending_evidence_text": ending,
            },
            presence_updates=[{"character_id": "daren", "action": "enter"}],
        )
        data["extracted"]["relationship_review"] = [{
            "character_id": "daren",
            "changed": False,
            "reason": "Новая встреча, устойчивый числовой сдвиг пока не доказан.",
            "numeric_result": "no_numeric_dimension_justified",
        }]
        # Both the public Pydantic request model and the real HTTP endpoint
        # must preserve the actor; a direct commit helper alone missed this bug.
        request = CommitTurnRequest.model_validate(data)
        assert request.extracted.scene_progression.character_id == "daren"
        with TestClient(app) as client:
            result = client.post(f"/sessions/{sid}/turns", json=data)
        assert result.status_code == 200, result.text
        assert result.json()["turn_number"] == 1


def test_offscreen_major_action_advances_cast_without_intent_or_pov_presence():
    from app.models import CommitTurnRequest

    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_offscreen=True)
        raw = "(читать книгу)"
        manifest, context = _prepare(sid, raw)
        assert "cast:independent" in {
            target["target_id"]
            for target in context["progression_contract"]["eligible_targets"]
        }
        ending = "В заброшенном здании Дарен обнаружил новое повреждение перехода."
        data = _payload(
            manifest,
            raw,
            "Кайр продолжал читать дома. " + ending,
            scene_progressed=True,
            proof={
                "target": "cast:independent",
                "character_id": "daren",
                "kind": "npc_action",
                "action": "Дарен самостоятельно обследовал переход.",
                "end_state_change": "Дарен получил новое доказательство повреждения.",
                "ending_kind": "concrete_next_pressure",
                "ending_evidence_text": ending,
            },
            chronology=[{
                "event": ending,
                "actor_character_id": "daren",
                "location": "заброшенное здание",
                "importance": "major",
                "consequences": ["Дарен продолжит собственное расследование."],
            }],
        )
        model_data = CommitTurnRequest.model_validate(data).model_dump()
        result = commit_turn_request(sid, model_data)
        assert result["turn_number"] == 1
        root = storage.SESSIONS_DIR / sid
        saved_chronology = storage._read_json(root / "chronology.json", [])
        assert any(row.get("actor_character_id") == "daren" for row in saved_chronology)
        entry = next(row for row in saved_chronology if row.get("actor_character_id") == "daren")
        assert entry.get("location") == "заброшенное здание"
        assert "kair" not in entry.get("participants_present", [])
        state = storage._read_json(root / "state.json", {})
        assert "daren" not in state.get("current", {}).get("present_characters", [])
        assert not state.get("npc_intents")


def test_offscreen_actor_does_not_inherit_pov_location_or_witnesses():
    from app.session_runtime import _normalise_chronology_events

    cards = [
        {"character_id": "kair", "name": "Кайр", "is_pov": True},
        {"character_id": "daren", "name": "Дарен"},
    ]
    state = {"current": {
        "location": "дом Кайра",
        "present_characters": ["kair"],
        "date": "08.10.2026",
    }}
    rows = _normalise_chronology_events(
        [{"event": "Дарен нашёл след.", "actor_character_id": "daren", "importance": "major"}],
        turn_number=3, state=state, cards=cards,
    )
    assert rows[0]["actor_character_id"] == "daren"
    assert "location" not in rows[0]
    assert "participants_present" not in rows[0]


def test_independent_actor_proof_without_real_action_is_rejected():
    from app.models import CommitTurnRequest

    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp, with_offscreen=True)
        raw = "(читать книгу)"
        manifest, _ = _prepare(sid, raw)
        ending = "Кайр заметил свежую трещину на стене."
        data = _payload(
            manifest, raw, ending,
            scene_progressed=True,
            proof={
                "target": "cast:independent",
                "character_id": "daren",
                "kind": "npc_action",
                "action": "Дарен якобы изучал переход.",
                "end_state_change": "Трещина обнаружена.",
                "ending_kind": "concrete_next_pressure",
                "ending_evidence_text": ending,
            },
            chronology=[{"event": ending, "importance": "major"}],
        )
        with pytest.raises(HTTPException) as exc:
            commit_turn_request(sid, CommitTurnRequest.model_validate(data).model_dump())
        assert exc.value.detail["code"] == "SCENE_NO_MEANINGFUL_PROGRESSION"


def test_optional_api_token_protects_private_session_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with tempfile.TemporaryDirectory() as tmp:
        sid = _setup(tmp)
        with TestClient(app) as client:
            monkeypatch.delenv("ROMAN_API_TOKEN", raising=False)
            assert client.get(f"/sessions/{sid}").status_code == 200

            monkeypatch.setenv("ROMAN_API_TOKEN", "integration-test-secret")
            assert client.get("/health").status_code == 200
            assert client.get("/openapi.json").status_code == 200
            assert client.get(f"/sessions/{sid}").status_code == 401
            assert client.post(f"/sessions/{sid}/turn-packet", json={"user_input": "(читать)"}).status_code == 401
            assert client.get(
                f"/sessions/{sid}",
                headers={"Authorization": "Bearer integration-test-secret"},
            ).status_code == 200
            assert client.get(
                f"/sessions/{sid}",
                headers={"X-Roman-Token": "integration-test-secret"},
            ).status_code == 200
            assert client.get(
                f"/sessions/{sid}",
                headers={"Authorization": "Bearer wrong-key"},
            ).status_code == 401
