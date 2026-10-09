import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import relationship_file_runtime, session_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def novel():
    return {
        "novel_id": "relationship-antifreeze",
        "title": "Relationship Antifreeze",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "rina"},
        "characters": [
            {"character_id": "rina", "name": "Рината", "is_pov": True},
            {
                "character_id": "adrian",
                "name": "Эдриан",
                "relationships": [
                    {
                        "target_character_id": "rina",
                        "relationship_type": "давно знакомы",
                        "current_dynamic": "Доверяет, но всё ещё ждёт, что Рината снова уйдёт от разговора.",
                        "dimensions": [
                            {"label": "доверие", "value": 40},
                            {"label": "близость", "value": 25},
                        ],
                    }
                ],
            },
        ],
        "starting_state": {
            "pov": {"character_id": "rina"},
            "current": {
                "date": "05.10.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["rina", "adrian"],
            },
        },
    }


def read_all_pending(session_id: str, manifest):
    start = 1 if manifest.get("first_chunk_included") else 0
    for index in range(start, manifest["chunk_count"]):
        storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)


def read_context(session_id: str):
    manifest = session_runtime.prepare_turn_packet(session_id, "(посмотреть на Эдриана)")
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        parts.append(storage.get_turn_packet_chunk(session_id, manifest["packet_id"], index)["content"])
    return json.loads("".join(parts))


def payload(manifest, user_input: str, *, review=None, updates=None):
    return {
        "packet_id": manifest["packet_id"],
        "user_input": user_input,
        "scene_output": "Эдриан остаётся рядом и реагирует на Ринату.",
        "extracted": {
            "scene_builder_reviewed": True,
            "persistence_reviewed": True,
            "knowledge_reviewed": True,
            "chronology": [],
            "knowledge_journal_add": [],
            "npc_intent_updates": [],
            "npc_relationship_updates": [],
            "story_thread_updates": [],
            "presence_updates": [],
            "relationship_review": review or [],
            "relationship_updates": updates or [],
            "state_patch": {},
            "character_upserts": [],
        },
    }


def test_numeric_shift_commits_without_relationship_review():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "Спасибо.", request_id="no-review")
        read_all_pending(sid, manifest)
        assert manifest["relationship_review_required"] is False
        data = payload(manifest, "Спасибо.", updates=[{
            "character_id": "adrian", "reason": "Благодарность усилила доверие.",
            "dimensions": [{"label": "доверие", "delta": 1}],
        }])
        data["extracted"].pop("relationship_review")
        commit_turn_request(sid, data)
        root = storage.SESSIONS_DIR / sid
        assert storage._read_json(root / "relationships.json", {})["npc_to_pov"]["adrian"]["dimensions"]["доверие"]["value"] == 41
        assert "relationship_review" not in storage._read_turns(root)[-1]["extracted"]


def test_old_pending_review_marker_and_rows_do_not_block_valid_updates():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        manifest = prepare_turn_request(sid, "Спасибо.", request_id="legacy-review")
        read_all_pending(sid, manifest)
        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        packet["relationship_review_required"] = True
        storage._write_json(root / "turn_packet.json", packet)
        result = commit_turn_request(sid, payload(manifest, "Спасибо.",
            review=[{"character_id": "adrian", "changed": False, "reason": "Старый отчёт"}],
            updates=[{"character_id": "adrian", "reason": "Благодарность усилила доверие.",
                      "dimensions": [{"label": "доверие", "delta": 1}]}]))
        assert result["turn_number"] == 1
        assert "relationship_review" not in storage._read_turns(root)[-1]["extracted"]


def test_legacy_prose_update_does_not_create_live_relationship_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        before = storage._read_json(root / "relationships.json", {})
        before_values = {
            label: item["value"]
            for label, item in before["npc_to_pov"]["adrian"]["dimensions"].items()
        }

        manifest = prepare_turn_request(sid, "Я пришла, как обещала.", request_id="dynamic-only")
        read_all_pending(sid, manifest)
        result = commit_turn_request(
            sid,
            payload(
                manifest,
                "Я пришла, как обещала.",
                review=[{
                    "character_id": "adrian",
                    "changed": True,
                    "reason": "Выполненное обещание немного изменило текущую динамику.",
                }],
                updates=[{
                    "character_id": "adrian",
                    "reason": "Выполненное обещание уменьшило ожидание очередного отказа.",
                    "dynamic": "После выполненного обещания меньше ждёт от Ринаты очередного отказа.",
                }],
            ),
        )
        assert result["turn_number"] == 1

        after = storage._read_json(root / "relationships.json", {})
        after_relation = after["npc_to_pov"]["adrian"]
        after_values = {
            label: item["value"]
            for label, item in after_relation["dimensions"].items()
        }
        assert after_values == before_values
        assert "dynamic" not in after_relation
        assert "dynamic_last_change" not in after_relation
        assert all(set(item) == {"value"} for item in after_relation["dimensions"].values())
        assert relationship_file_runtime.normalize_store(after)["npc_to_pov"]["adrian"] == after_relation

        turns = storage._read_turns(root)
        assert "relationship_review" not in turns[-1].get("extracted", {})

        context = read_context(sid)
        assert context["relationships"] == after
        assert context["relationship_lens"]["values_path"] == "relationships.npc_to_pov"


def test_starting_prose_does_not_invent_numeric_dimensions():
    cards = [
        {"character_id": "rina", "name": "Рината", "is_pov": True},
        {
            "character_id": "tessa",
            "name": "Тэсса",
            "relationships": [{
                "target_character_id": "rina",
                "relationship_type": "настороженное знакомство",
                "current_dynamic": "Пока присматривается к Ринате и не решила, доверяет ли ей.",
                "dimensions": [],
            }],
        },
    ]
    store = relationship_file_runtime.build_initial_store(
        cards,
        {"pov": {"character_id": "rina"}},
        "rina",
    )
    assert store["npc_to_pov"]["tessa"]["dimensions"] == {}
    assert set(store["npc_to_pov"]["tessa"]) == {"dimensions"}


def apply(store, updates):
    return relationship_file_runtime.apply_updates(store, updates, cards=novel()["characters"],
        pov_id="rina", turn_number=1, participant_ids=["adrian"])


def initial_store():
    n = novel()
    return relationship_file_runtime.build_initial_store(n["characters"], n["starting_state"], "rina")


def test_numeric_changes_need_no_reason_or_critical_event_gate():
    store = initial_store()
    after = apply(store, [{"character_id": "adrian", "dimensions": [{"label": "доверие", "delta": -40}]}])
    assert "доверие" not in after["npc_to_pov"]["adrian"]["dimensions"]
    after = apply(after, [{"character_id": "adrian", "dimensions": [{"label": "недоверие", "value": 1}]}])
    assert after["npc_to_pov"]["adrian"]["dimensions"]["недоверие"] == {"value": 1}
    assert store == initial_store()


def test_cap_is_checked_on_final_state_independent_of_dimension_order():
    store = initial_store()
    store["npc_to_pov"]["adrian"]["dimensions"] = {f"ось{i}": {"value": 1} for i in range(10)}
    for dims in ([{"label": "новая", "value": 1}, {"label": "ось0", "delta": -1}],
                 [{"label": "ось0", "delta": -1}, {"label": "новая", "value": 1}]):
        after = apply(store, [{"character_id": "adrian", "reason": "Смена отношения", "dimensions": dims}])
        assert len(after["npc_to_pov"]["adrian"]["dimensions"]) == 10
        assert "ось0" not in after["npc_to_pov"]["adrian"]["dimensions"]
    with pytest.raises(ValueError, match="RELATIONSHIP_DIMENSION_LIMIT"):
        apply(store, [{"character_id": "adrian", "reason": "Сдвиг", "dimensions": [{"label": "лишняя", "value": 1}]}])


@pytest.mark.parametrize("updates,code", [
    ([{"character_id": "adrian", "reason": "Сдвиг", "dimensions": [{"label": "доверие", "delta": float("nan")}]}], "RELATIONSHIP_EXISTING_DIMENSION_DELTA_REQUIRED"),
])
def test_numeric_invariants_remain_enforced(updates, code):
    with pytest.raises(ValueError, match=code):
        apply(initial_store(), updates)


def test_neutral_first_encounter_creates_empty_record_and_rollback_removes_it():
    from app.turn_rollback import rollback_last_turn
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        n = novel()
        n["characters"][1].pop("relationships")
        sid = storage.create_session(n)["session_id"]
        root = storage.SESSIONS_DIR / sid
        before = storage._read_json(root / "relationships.json", {})
        manifest = prepare_turn_request(sid, "Здравствуйте.", request_id="first-encounter")
        read_all_pending(sid, manifest)
        data = payload(manifest, "Здравствуйте.")
        data["extracted"].pop("relationship_review")
        commit_turn_request(sid, data)
        after = storage._read_json(root / "relationships.json", {})
        assert after["npc_to_pov"]["adrian"] == {"dimensions": {}}
        assert relationship_file_runtime.normalize_store(after) == after
        assert relationship_file_runtime.footer_rows(after, ["adrian"]) == {}
        assert relationship_file_runtime.rebuild_from_turns(n, n["characters"], storage._read_turns(root)) == after
        rollback_last_turn(sid, 1, True)
        assert storage._read_json(root / "relationships.json", {}) == before


def test_dynamic_and_numeric_changes_survive_retry_continuation_read_and_exact_rollback():
    from app import continuation_runtime
    from app.turn_rollback import rollback_last_turn
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        n = novel()
        sid = storage.create_session(n)["session_id"]
        root = storage.SESSIONS_DIR / sid
        before = storage._read_json(root / "relationships.json", {})
        manifest = prepare_turn_request(sid, "Я здесь.", request_id="combined")
        read_all_pending(sid, manifest)
        data = payload(manifest, "Я здесь.", updates=[{
            "character_id": "adrian", "reason": "POV пришла, как обещала", "dynamic": "Теперь охотнее полагается на неё",
            "dimensions": [{"label": "доверие", "delta": 1}, {"label": "настороженность", "value": 1}],
        }])
        commit_turn_request(sid, data)
        after = storage._read_json(root / "relationships.json", {})
        assert commit_turn_request(sid, data)["already_committed"] is True
        assert storage._read_json(root / "relationships.json", {}) == after
        parts = continuation_runtime._load_source_parts(sid)
        assert parts["relationships"] == after
        continuation_runtime._save_migration(sid, {"source_turn": 1, "final_package": {
            "current": parts["state"]["current"], "threads": {},
            "memory_normalized": parts["memory"], "chronology_normalized": parts["chronology"],
        }})
        continued = continuation_runtime.create_continuation_session(sid)
        new_root = storage.SESSIONS_DIR / continued["session_id"]
        assert storage._read_json(new_root / "relationships.json", {}) == after
        assert relationship_file_runtime.rebuild_from_turns(n, n["characters"], storage._read_turns(root)) == after
        rollback_last_turn(sid, 1, True)
        assert storage._read_json(root / "relationships.json", {}) == before


@pytest.mark.parametrize("played", [False, True])
def test_v1_upgrade_preserves_numbers_and_does_not_revive_stale_setup_dynamic(played):
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        n = novel()
        sid = storage.create_session(n)["session_id"]
        root = storage.SESSIONS_DIR / sid
        old = initial_store()
        old["version"] = 1
        old["npc_to_pov"]["adrian"]["dynamic"] = "Старая длинная динамика"
        old["npc_to_pov"]["adrian"]["dynamic_last_change"] = {"turn": 1, "reason": "Старый отчёт"}
        old["npc_to_pov"]["adrian"]["dimensions"]["доверие"]["last_change"] = {"turn": 1, "delta": 2, "reason": "Длинное объяснение"}
        storage._write_json(root / "relationships.json", old)
        if played:
            meta = storage._read_json(root / "meta.json", {})
            meta["turn_number"] = 20
            storage._write_json(root / "meta.json", meta)
        upgraded = relationship_file_runtime.load(root, cards=n["characters"], state=n["starting_state"], pov_id="rina")
        assert upgraded["npc_to_pov"]["adrian"]["dimensions"] == initial_store()["npc_to_pov"]["adrian"]["dimensions"]
        assert "dynamic" not in upgraded["npc_to_pov"]["adrian"]
        assert upgraded["version"] == 3
        assert storage._read_json(root / "relationships.json", {}) == upgraded


def test_legacy_split_updates_remain_replayable_for_rollback():
    n = novel()
    update = {"character_id": "adrian", "reason": "Старый сохранённый сдвиг", "dimensions": [{"label": "доверие", "delta": 3}]}
    rebuilt = relationship_file_runtime.rebuild_from_turns(n, n["characters"], [{
        "turn_number": 1, "extracted": {"relationship_updates": [update, update]},
    }])
    assert rebuilt["npc_to_pov"]["adrian"]["dimensions"]["доверие"]["value"] == 46


def test_offscreen_npc_cannot_receive_numeric_update_without_participating():
    with pytest.raises(ValueError, match="RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC"):
        relationship_file_runtime.apply_updates(initial_store(), [{
            "character_id": "adrian", "reason": "Сдвиг", "dimensions": [{"label": "доверие", "delta": 1}],
        }], cards=novel()["characters"], pov_id="rina", turn_number=1, participant_ids=[])


def test_public_commit_first_impression_and_remote_change_use_the_file_only():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        n = novel()
        n["characters"][1].pop("relationships")
        n["starting_state"]["current"]["present_characters"] = ["rina"]
        n["starting_state"]["current"]["remote_characters"] = ["adrian"]
        sid = storage.create_session(n)["session_id"]
        root = storage.SESSIONS_DIR / sid
        assert not storage._read_json(root / "relationships.json", {})["npc_to_pov"]
        manifest = prepare_turn_request(sid, "Здравствуйте по телефону.", request_id="remote-first")
        read_all_pending(sid, manifest)
        data = payload(manifest, "Здравствуйте по телефону.", updates=[{
            "character_id": "adrian", "dimensions": [{"label": "любопытство", "value": 2}],
        }])
        commit_turn_request(sid, data)
        current = storage._read_json(root / "relationships.json", {})
        assert current["npc_to_pov"]["adrian"] == {"dimensions": {"любопытство": {"value": 2}}}
        assert commit_turn_request(sid, data)["already_committed"] is True
        context = read_context(sid)
        assert context["relationships"] == current
        assert context["relationship_lens"]["footer_character_ids"] == ["adrian"]
        assert "last_change" not in json.dumps(current)


def test_existing_file_beats_starting_profile_and_upgrade_never_replays_history(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        n = novel()
        sid = storage.create_session(n)["session_id"]
        root = storage.SESSIONS_DIR / sid
        old = initial_store()
        old["version"] = 2
        old["npc_to_pov"]["adrian"]["dimensions"]["доверие"] = {
            "value": 19, "last_change": {"turn": 9, "reason": "причина " * 100},
        }
        old["npc_to_pov"]["adrian"]["dynamic"] = "длинное описание " * 100
        storage._write_json(root / "relationships.json", old)
        with monkeypatch.context() as patch:
            def unexpected_replay(*args, **kwargs):
                raise AssertionError("A current file must not replay old turns")
            patch.setattr(storage, "_read_turns", unexpected_replay)
            upgraded = relationship_file_runtime.load(root, cards=n["characters"], state=n["starting_state"], pov_id="rina")
        assert upgraded["npc_to_pov"]["adrian"]["dimensions"]["доверие"] == {"value": 19}
        assert "dynamic" not in upgraded["npc_to_pov"]["adrian"]
        assert read_context(sid)["relationships"] == upgraded
