"""Canonical generator documents must remain usable by the Roman turn API."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import operation_service, session_documents as documents, session_runtime, storage
from app.profile_templates import normalize_character_profile, normalize_novel_profile
from app.novel_drafts import _normalise_starting_state_for_session


@pytest.fixture
def story(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    return {
        "novel_id": "document-layout", "title": "Старая история", "version": 5,
        "novel": {"pov_character": "pov", "genres": ["мистика"], "start": "Утро в кафе"},
        "source_intake": [{"block_id": "original", "raw_text": "  Дословный RAW.\nНе сокращать.  "}],
        "characters": [
            {"character_id": "pov", "name": "Елена", "is_pov": True},
            {"character_id": "ren", "name": "Рен", "age": 26, "habits": "Крутит кольцо",
             "relationships": [{"target_character_id": "pov", "dimensions": [{"label": "интерес", "value": 12}]}]},
            {"character_id": "away", "name": "Николь", "notes": "Скрытая подробность отсутствующего NPC"},
        ],
        "starting_state": {"pov": {"character_id": "pov", "clothing": "платье"},
            "current": {"date": "14.10.2026", "time": "09:00", "period": "утро", "location": "Кафе",
                        "present_characters": ["pov", "ren"]}},
    }


def create(story):
    sid = storage.create_session(story)["session_id"]
    return sid, storage.SESSIONS_DIR / sid


def prepare(sid, text="Продолжаю разговор", request_id="native-turn"):
    manifest = operation_service.prepare_turn_request(sid, text, request_id=request_id)
    parts = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        part = storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)
        assert len(part["content"]) <= 16_000
        parts.append(part["content"])
    return manifest, json.loads("".join(parts))


def commit(sid, manifest, **changes):
    payload = {"packet_id": manifest["packet_id"], "user_input": "Продолжаю разговор",
        "scene_output": "**Рен** - Я услышал тебя.",
        "extracted": {"scene_builder_reviewed": True, "persistence_reviewed": True, "knowledge_reviewed": True, **changes}}
    result = operation_service.commit_turn_request(sid, payload)
    return result, payload


def test_native_turn_seed_skips_legacy_layers_without_losing_story_context(story):
    sid, _ = create(story)
    seed = session_runtime.build_turn_context(sid, "Продолжаю разговор", lean_native=True)
    assert seed["runtime_rules"] and seed["scene_builder"]
    assert seed["relevant_character_ids"] == ["pov", "ren"]
    assert seed["novel"]["start"] == "Утро в кафе"
    # The active pipeline loads these once from canonical participant documents.
    for duplicate in ("scene_characters", "character_registry", "author_context",
                      "character_memory", "relationship_policy", "persistence_contract"):
        assert duplicate not in seed

    _, packet = prepare(sid)
    assert set(packet["character_memory"]) == {"pov", "ren"}
    assert {row["character_id"] for row in packet["character_cards"]} == {"pov", "ren"}
    assert packet["chronology"]["дни"] == {}
    assert packet["runtime_rules"] and packet["scene_builder"]
    assert "scene_characters" not in packet


def test_native_familiarity_does_not_open_offscreen_personal_memory(story, monkeypatch):
    sid, root = create(story)
    original = documents.read
    reads = []

    def tracked(path, default=None):
        reads.append(str(path))
        if path.parent.name == "память" and path.name == "away.json":
            raise AssertionError("Offscreen personal memory was read by native familiarity")
        return original(path, default)

    monkeypatch.setattr(documents, "read", tracked)
    context = session_runtime.build_turn_context(sid, "Разговор с Реном", lean_native=True)
    assert context["relevant_character_ids"] == ["pov", "ren"]
    assert any(path.endswith("память/pov.json") for path in reads)
    assert not any(path.endswith("память/away.json") for path in reads)


def test_migrated_familiarity_rechecks_old_turns_despite_stored_status(story):
    sid, root = create(story)
    # An old session can have an already-filled but incomplete status.
    state = storage._read_json(root / "state.json", {})
    state.setdefault("characters", {}).setdefault("ren", {})["pov_familiarity"] = {
        "status": "not_encountered", "source": "none",
    }
    storage._write_json(root / "state.json", state)
    turns_path = documents.file_path(root, "turns.jsonl")
    turns_path.write_text("".join(
        json.dumps({"turn_number": n,
                    "user_input": "Рен представился Елене" if n == 2 else "Другой ход",
                    "scene_output": "Встреча" if n == 2 else "Текущие события",
                    "extracted": {}}, ensure_ascii=False) + "\n"
        for n in range(1, 101)
    ), encoding="utf-8")
    meta = storage._read_json(root / "meta.json", {})
    meta["turn_number"] = 100
    meta.pop("_familiarity_index_version", None)
    storage._write_json(root / "meta.json", meta)
    session_runtime.build_turn_context(sid, "Продолжаю", lean_native=True)
    first = storage._read_json(root / "state.json", {})["characters"]["ren"]["pov_familiarity"]
    assert first["status"] == "acquainted"
    assert storage._read_json(root / "meta.json", {})["_familiarity_index_version"] == 1
    session_runtime.build_turn_context(sid, "Продолжаю", lean_native=True)
    later = storage._read_json(root / "state.json", {})["characters"]["ren"]["pov_familiarity"]
    assert later["status"] == "acquainted"


def test_private_evidence_index_rebuilds_on_archive_rewrite(story):
    from app import private_knowledge_runtime as guard
    sid, root = create(story)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    archive = documents.file_path(root, "turns.jsonl")
    meta = storage._read_json(root / "meta.json", {})
    meta["turn_number"] = 1
    storage._write_json(root / "meta.json", meta)

    def replace_message(word):
        archive.write_text(json.dumps({
            "turn_number": 1,
            "user_input": f"(написать Рену: секретное слово {word})",
            "scene_output": "Рен получил сообщение.",
            "extracted": {},
        }, ensure_ascii=False) + "\n", encoding="utf-8")

    replace_message("фуксия")
    original = guard._private_records(root, cards)
    assert any("фуксия" in record.get("payload", "") for record in original)
    assert (root / guard.PRIVATE_INDEX_FILE).is_file()
    replace_message("бирюза")
    updated = guard._private_records(root, cards)
    assert any("бирюза" in record.get("payload", "") for record in updated)
    assert not any("фуксия" in record.get("payload", "") for record in updated)


def test_native_request_id_index_keeps_exact_retry_and_survives_rollback(story):
    from app.turn_duplicate_guard import REQUEST_INDEX_FILE
    sid, root = create(story)
    manifest, _ = prepare(sid, request_id="req-777")
    result, payload = commit(sid, manifest)
    assert result["turn_number"] == 1
    index = documents.read(root / REQUEST_INDEX_FILE, {})
    assert index["requests"]["req-777"]["turn_number"] == 1
    replay = operation_service.prepare_turn_request(
        sid, payload["user_input"], request_id="req-777",
    )
    assert replay["already_committed_duplicate"] is True
    assert replay["scene_output"] == payload["scene_output"]
    operation_service.rollback_last_turn_request(sid, {
        "expected_turn_number": 1,
        "expected_turn_id": operation_service.current_turn_identity(root),
        "confirm": True,
    })
    again = operation_service.prepare_turn_request(
        sid, payload["user_input"], request_id="req-777",
    )
    assert again.get("already_committed_duplicate") is not True


def test_exact_ten_template_set_and_native_session_files(story):
    assert {p.name for p in documents.TEMPLATE_ROOT.glob("*.json")} == set(documents.FILES)
    assert len(documents.FILES) == 10
    sid, root = create(story)
    for name in ("сессия.json", "анкета.json", "персонажи.json", "state.json", "отношения.json",
                 "локации.json", "скрытый_лор_и_крючки.json", "хронология.json", "ходы.jsonl"):
        assert (root / name).is_file(), name
    for name in ("meta.json", "characters.json", "memory.json", "chronology.json", "relationships.json", "source.json"):
        assert not (root / name).exists(), name
    for cid in ("pov", "ren", "away"):
        assert documents.read(root / f"карточки/{cid}.json")["id"] == cid
        assert documents.read(root / f"память/{cid}.json")["персонаж_id"] == cid
    assert documents.read(root / "сессия.json")["id"] == sid
    assert documents.read(root / "state.json")["pov_id"] == "pov"
    assert storage._read_json(root / "source.json", {})["source_intake"] == story["source_intake"]


def test_russian_profile_fields_are_accepted_by_existing_setup_api():
    card = normalize_character_profile({"id": "pov", "имя": "Елена", "is_pov": True,
        "рост_см": 165, "внутренний_конфликт": "Боится потерять работу", "нравится": "Скрипка"})
    native = documents.card_to_native(card)
    assert native["рост_см"] == 165
    assert native["внутренний_конфликт"] == "Боится потерять работу"
    assert native["нравится"] == "Скрипка"
    assert card["is_pov"] is True
    novel = normalize_novel_profile({"название": "История", "pov_character": "pov", "дата_начала": "14.10.2026"})
    assert novel["pov_character"] == "pov"
    assert documents.novel_to_native({"novel": novel})["дата_начала"] == "14.10.2026"


def test_every_old_chronology_event_and_personal_fact_is_read(story):
    sid, root = create(story)
    events = [{"event_id": f"world-{i}", "date": "01.09.2026", "period": "вечер", "turn_number": i,
               "event": f"Установленный факт мира {i}", "participants": ["away"]} for i in range(1, 141)]
    storage._write_json(root / "chronology.json", events)
    memory = storage._read_json(root / "memory.json", {})
    for cid in ("pov", "ren", "away"):
        memory["characters"][cid]["knowledge_journal"] = [
            {"entry_id": f"{cid}-{i}", "date": "01.09.2026", "period": "вечер", "turn": i,
             "text": f"Со слов свидетеля: личный факт {cid} {i}"} for i in range(1, 141)]
    storage._write_json(root / "memory.json", memory)
    manifest, packet = prepare(sid)
    chronology = packet["chronology"]["дни"]["01.09.2026"]["вечер"]
    assert chronology == [row["event"] for row in events]
    assert set(packet["character_memory"]) == {"pov", "ren"}
    for cid in ("pov", "ren"):
        knowledge = packet["character_memory"][cid]["знания"]["01.09.2026"]
        assert knowledge == [row["text"] for row in memory["characters"][cid]["knowledge_journal"]]
    assert "личный факт away" not in json.dumps(packet["character_memory"], ensure_ascii=False)
    assert "_runtime" not in json.dumps(packet["character_memory"], ensure_ascii=False)
    assert "chronology_recent" not in packet
    assert manifest["chunk_chars_max"] == 16_000


def test_commit_retry_resume_and_rollback_preserve_native_data(story):
    sid, root = create(story)
    manifest, _ = prepare(sid)
    baseline = {name: storage._read_json(root / name, {}) for name in ("state.json", "memory.json", "chronology.json", "relationships.json")}
    result, payload = commit(sid, manifest,
        chronology=[{"event_id": "exchange", "event": "Елена сообщила Рену свою версию события", "date": "14.10.2026", "period": "утро"}],
        knowledge_journal_add=[{"character_id": "ren", "text": "Со слов Елены: она никогда не делала пирсинг."}],
        relationship_updates=[{"character_id": "ren", "dimensions": [{"label": "интерес", "delta": 2}]}])
    assert result["turn_number"] == 1
    own = documents.read(root / "память/ren.json")
    assert own["знания"]["14.10.2026"][-1] == "Со слов Елены: она никогда не делала пирсинг."
    assert documents.read(root / "отношения.json")["персонажи"]["ren"]["к_POV"]["параметры"]["интерес"] == 14
    retry = operation_service.commit_turn_request(sid, payload)
    assert retry["already_committed"] is True
    assert len(storage._read_turns(root)) == 1
    resumed = session_runtime.continue_session(sid)
    assert resumed["last_committed_turn"]["scene_output"] == payload["scene_output"]
    operation_service.rollback_last_turn_request(sid, {"expected_turn_number": 1,
        "expected_turn_id": operation_service.current_turn_identity(root), "confirm": True})
    for name, before in baseline.items():
        assert storage._read_json(root / name, {}) == before, name
    assert documents.read(root / "сессия.json")["последний_ход"] == 0


def test_rollback_removes_new_character_personal_files(story):
    sid, root = create(story)
    manifest, _ = prepare(sid)
    commit(sid, manifest, character_upserts=[{"character_id": "new", "name": "Лиам", "story_function": "свидетель в кафе"}])
    assert (root / "карточки/new.json").is_file()
    assert (root / "память/new.json").is_file()
    operation_service.rollback_last_turn_request(sid, {"expected_turn_number": 1,
        "expected_turn_id": operation_service.current_turn_identity(root), "confirm": True})
    assert not (root / "карточки/new.json").exists()
    assert not (root / "память/new.json").exists()


def test_existing_v5_story_migrates_once_with_source_turns_and_ids(story):
    old = deepcopy(story)
    old["version"] = 4
    sid, root = create(old)
    memory = storage._read_json(root / "memory.json", {})
    memory["characters"]["ren"]["knowledge_journal"] = [{"entry_id": "original-id", "turn": 7,
        "date": "02.09.2026", "period": "вечер", "text": "Со слов Елены: это её предположение."}]
    storage._write_json(root / "memory.json", memory)
    source = storage._read_json(root / "source.json", {})
    source["version"] = 5
    storage._write_json(root / "source.json", source)
    assert documents.ensure_native(root) is True
    assert documents.ensure_native(root) is False
    assert storage._read_json(root / "memory.json", {}) == memory
    assert storage._read_json(root / "source.json", {})["source_intake"] == source["source_intake"]
    assert storage._read_json(root / "meta.json", {})["session_id"] == sid
    assert not (root / "memory.json").exists()


def test_native_memory_and_chronology_edits_are_authoritative(story):
    sid, root = create(story)
    own = documents.read(root / "память/ren.json")
    own["знания"]["13.10.2026"] = ["Увидел письмо с адресом отправителя."]
    (root / "память/ren.json").write_text(json.dumps(own, ensure_ascii=False))
    history = documents.read(root / "хронология.json")
    history["дни"]["13.10.2026"] = {"вечер": ["Письмо было доставлено в кафе."]}
    (root / "хронология.json").write_text(json.dumps(history, ensure_ascii=False))
    _, packet = prepare(sid)
    assert packet["character_memory"]["ren"]["знания"]["13.10.2026"] == own["знания"]["13.10.2026"]
    assert packet["chronology"]["дни"]["13.10.2026"] == history["дни"]["13.10.2026"]


def test_native_launch_state_and_native_turn_state_patch_are_accepted(story):
    native = documents.state_to_native(story["starting_state"])
    normalized = _normalise_starting_state_for_session({**story, "starting_state": native})
    assert normalized["starting_state"]["current"]["present_characters"] == ["pov", "ren"]
    sid, root = create(story)
    manifest, _ = prepare(sid)
    commit(sid, manifest, state_patch={"точное_время": "09:05", "локация": {"id": None, "название": "Улица"}})
    state = documents.read(root / "state.json")
    assert state["точное_время"] == "09:05"
    assert state["локация"]["название"] == "Улица"
    assert [row["id"] for row in state["физически"]] == ["pov", "ren"]


def test_structured_and_nonstandard_old_records_survive_conversion(story):
    sid, root = create(story)
    memory = storage._read_json(root / "memory.json", {})
    memory["characters"]["ren"]["knowledge"] = [{"fact_id": "structured", "fact": {"со_слов": "Елены", "версия": "Ей сказали об отъезде"}, "learned_turn": 3}]
    memory["characters"]["ren"]["experiences"] = [{"event_id": "old", "action": "Сохранил письмо", "result": "Не открыл", "turn": 3}]
    storage._write_json(root / "memory.json", memory)
    assert storage._read_json(root / "memory.json", {}) == memory
    _, packet = prepare(sid)
    blob = json.dumps(packet["character_memory"]["ren"], ensure_ascii=False)
    assert "Ей сказали об отъезде" in blob
    assert "Сохранил письмо" in blob


@pytest.mark.parametrize("missing", ["хронология.json", "память/ren.json", "память/pov.json"])
def test_scene_cannot_prepare_without_required_chronology_or_own_knowledge(story, missing):
    sid, root = create(story)
    (root / missing).unlink()
    with pytest.raises(HTTPException) as caught:
        prepare(sid)
    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "SESSION_DOCUMENT_REQUIRED", "path": missing,
        "message": "Cannot write a scene without its chronology and participant knowledge."}
    assert not (root / "turn_packet.json").exists()


def test_migration_failure_keeps_legacy_story_readable(story, monkeypatch):
    old = deepcopy(story)
    old["version"] = 4
    sid, root = create(old)
    source = storage._read_json(root / "source.json", {})
    source["version"] = 5
    storage._write_json(root / "source.json", source)
    before = {p.relative_to(root): p.read_bytes() for p in root.glob("*.json")}
    from app import transactional_storage
    original = transactional_storage.os.replace
    failed = False
    def interrupt(source, target):
        nonlocal failed
        if Path(target).parent.name == "память" and not failed:
            failed = True
            raise OSError("injected storage interruption")
        return original(source, target)
    monkeypatch.setattr(transactional_storage.os, "replace", interrupt)
    with pytest.raises(OSError, match="injected storage"):
        documents.ensure_native(root)
    assert failed
    assert not documents.is_native(root)
    assert {p.relative_to(root): p.read_bytes() for p in root.glob("*.json")} == before
    assert storage._read_json(root / "source.json", {})["source_intake"] == source["source_intake"]



def test_generator_chronology_groups_real_events_by_date_and_period(story):
    """An omitted event period inherits the actual scene clock, never a made-up time."""
    sid, root = create(story)
    events = [
        {"event_id": "morning", "event": "Рен отдал письмо.", "date": "14.10.2026"},
        {"event_id": "night", "event": "Ночью пришёл ответ.", "date": "14.10.2026", "time": "22:15"},
        {"event_id": "explicit", "event": "Вчера выяснилась причина.", "date": "13.10.2026", "period": "вечер"},
        {"event_id": "old-unknown", "event": "Когда-то пропало письмо.", "date": "12.10.2026"},
    ]
    storage._write_json(root / "chronology.json", events)
    native = documents.read(root / "хронология.json")
    assert native["дни"]["14.10.2026"]["утро"] == ["Рен отдал письмо."]
    assert native["дни"]["14.10.2026"]["ночь"] == ["Ночью пришёл ответ."]
    assert native["дни"]["13.10.2026"]["вечер"] == ["Вчера выяснилась причина."]
    assert native["дни"]["12.10.2026"]["события"] == ["Когда-то пропало письмо."]
    assert storage._read_json(root / "chronology.json", []) == events
    _, packet = prepare(sid)
    assert packet["chronology"]["дни"] == native["дни"]


def test_generator_personal_memory_keeps_attributed_claim_separate_from_world_truth(story):
    sid, root = create(story)
    memory = storage._read_json(root / "memory.json", {})
    ren = memory["characters"]["ren"]
    ren["knowledge_journal"].append({
        "entry_id": "ren-heard", "date": "14.10.2026",
        "text": "Со слов Елены: письмо привёз её брат; истинность не проверял.",
    })
    ren["experiences"].append({
        "event_id": "ren-asked", "date": "14.10.2026",
        "summary": "Спросил Елену о письме; она уклонилась от ответа. Причину не знает.",
        "participants": ["pov", "ren"],
    })
    storage._write_json(root / "memory.json", memory)
    _, packet = prepare(sid)
    assert "Со слов Елены: письмо привёз её брат; истинность не проверял." in packet[
        "character_memory"]["ren"]["знания"]["14.10.2026"]
    assert packet["character_memory"]["ren"]["личные_события"]["14.10.2026"][-1]["с_кем"] == ["pov", "ren"]
    assert "письмо привёз её брат" not in json.dumps(
        packet["chronology"], ensure_ascii=False,
    )
    assert "письмо привёз её брат" not in json.dumps(
        packet["character_memory"]["pov"], ensure_ascii=False,
    )
    assert storage._read_json(root / "memory.json", {}) == memory


def test_scene_chronology_uses_clock_if_period_label_is_stale(story):
    sid, root = create(story)
    state = storage._read_json(root / "state.json", {})
    state["current"]["time"] = "23:05"
    state["current"]["period"] = "утро"
    storage._write_json(root / "state.json", state)
    storage._write_json(root / "chronology.json", [{
        "event_id": "late-hour", "event": "За окном стемнело.",
        "date": "14.10.2026",
    }])
    native = documents.read(root / "хронология.json")
    assert native["дни"]["14.10.2026"]["ночь"] == ["За окном стемнело."]
