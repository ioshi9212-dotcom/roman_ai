import json
import tempfile

import pytest
from pathlib import Path

from app import character_chunk_read, continuation_runtime, private_knowledge_runtime, runtime_access, session_runtime, simple_setup_runtime, storage
from app.operation_service import commit_turn_request, prepare_turn_request
from app.models import TurnCommit
from app.turn_rollback import rollback_last_turn


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def base_novel(version: int = 5):
    return {
        "novel_id": "rules-driven",
        "title": "Rules Driven",
        "version": version,
        "profile_schema": {"version": 1} if version >= 5 else {},
        "novel": {"pov_character": "pov", "genres": ["romance"]},
        "characters": [
            {"character_id": "pov", "name": "POV", "is_pov": True, "goals": ["скрыть секрет"]},
            {"character_id": "npc", "name": "NPC", "story_function": "possible romance", "goals": ["добиться ответа"]},
            {"character_id": "away", "name": "Away", "story_function": "old friend"},
        ],
        "starting_state": {
            "pov": {"character_id": "pov"},
            "current": {
                "date": "01.09.2026",
                "time": "10:00",
                "location": "room",
                "present_characters": ["pov", "npc"],
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



def test_active_runtime_is_author_rules_plus_scene_builder():
    docs = runtime_access.runtime_documents()
    sources = runtime_access.author_runtime_sources()
    assert docs["rules"] == sources["rules"]
    assert docs["scene_builder"] == sources["scene_builder"]

    rules = docs["rules"]
    builder = docs["scene_builder"]

    # Rules is technical transport/persistence; character behavior lives in Skin Builder.
    assert "Rules выполняет только техническую работу" in rules
    assert "Не задавай через Rules поведение персонажей" in rules
    assert "Skin Builder определяет поведение POV, NPC, мира" in rules
    assert builder.startswith("Формат scene_builder обязателен")
    assert "NPC - самостоятельный персонаж, а не функция для POV." in builder
    assert "Не выбирай поведение по принципу «как правильно»" in builder
    assert "Не усредняй противоречивые черты персонажа" in builder

    # Knowledge, continuity and causal cast selection stay explicit.
    assert "Частичный факт остаётся частичным." in rules
    assert "Источник знания должен существовать до использования знания." in rules
    assert "npc_active_intents[ID]" in rules
    assert "Смена темы intent не закрывает." in rules
    assert "present_characters" in rules
    assert "state.characters[ID]" in rules
    assert "Молчание не удаляет NPC" in builder
    assert "Личной мотивации NPC, его отношения, цели или story_function достаточно для инициативы." in rules
    assert "Режиссура сама находит логичный способ сталкивать важный каст" in rules
    assert "сколько ходов NPC отсутствовал" not in rules
    assert "Мир не ждёт POV" in builder

    for removed in (
        "ROUTINE",
        "STANDARD",
        "CINEMATIC",
        "ТОЧКА ОСТАНОВКИ СЦЕНЫ",
        "world_movement_context",
        "structural_movement_due",
    ):
        assert removed not in rules + "\n" + builder


def test_character_perception_and_scene_quality_stay_causal_without_pacing_modes():
    docs = runtime_access.runtime_documents()
    rules = docs["rules"]
    builder = docs["scene_builder"]

    assert "POV - живой участник сцены, не камера и не мебель." in builder
    assert "POV не нейтральный силуэт" in builder
    assert "NPC - самостоятельный персонаж, а не функция для POV." in builder
    assert "Не выбирай поведение по принципу «как правильно»" in builder
    assert "Заметное действие POV в сторону NPC получает естественную реакцию NPC" in builder
    assert "Ощущения POV не заменяют то, что физически происходит." in builder
    assert "На каждом значимом физическом переходе читатель должен понимать" in builder
    assert "Если новелла 18+, допустимы взрослые темы, включая секс." in builder
    assert "Служебные выводы из отношений, инициативы и persistence" in builder
    assert "Не растягивай один и тот же beat на несколько ходов без развития." in builder
    assert "не повторять один поцелуй пять ходов" in builder
    assert "Не давай психологический, моральный или авторский анализ." in builder
    assert "Не объясняй, не оправдывай и не оценивай поведение POV или NPC через режиссуру или нижний блок. Не хвали персонажей и их решения." in builder
    assert "Объём основной сцены: 1800–3000 непробельных символов." in builder
    assert "иногда достаточно одного выразительного визуального beat на 1–3 предложения" in builder
    assert "сам по себе не считается полноценной визуализацией" in builder
    assert "1800–3000" in builder

    assert "ROUTINE" not in builder
    assert "STANDARD" not in builder
    assert "CINEMATIC" not in builder
    assert "ТОЧКА ОСТАНОВКИ СЦЕНЫ" not in builder

def test_packet_has_no_hidden_director_guard_stack_and_rules_are_last():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        _, context = read_context(sid, "Где Away?")

        for removed in (
            "runtime_contract",
            "knowledge_guard",
            "knowledge_firewall_v5",
            "scene_logic_guardrails",
            "narrative_guardrails",
            "living_world",
            "relationship_policy",
            "cast_pressure",
            "story_pressure",
            "simple_knowledge_rules",
            "speaker_context",
            "character_knowledge_rule",
            "world_movement_context",
            "character_registry_instruction",
        ):
            assert removed not in context

        assert list(context)[-2:] == ["runtime_rules", "scene_builder"]
        active = {row["character_id"] for row in context["character_cards"]}
        assert active == {"pov", "npc"}
        assert "away" not in active
        assert "working_context_contract" not in context
        assert "persistence_contract" not in context
        assert "chronology_policy" not in context
        assert "## PERSISTENCE" in context["runtime_rules"]
        assert "POV clothing/inventory" in context["runtime_rules"]
        assert "NPC physical/runtime" in context["runtime_rules"]



def test_legacy_story_rule_is_absent_from_actual_turn_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["novel"]["story_rules"] = (
            "Если игрок не дал реплику, не придумывать её. "
            "NPC действуют самостоятельно."
        )
        sid = storage.create_session(novel)["session_id"]

        _, context = read_context(sid, "(посмотреть на NPC)")
        blob = json.dumps(context, ensure_ascii=False).casefold()
        assert "если игрок не дал реплику" not in blob
        assert "не придумывать её" not in blob
        assert "npc действуют самостоятельно" in blob
        assert "npc - самостоятельный персонаж" in context["scene_builder"].casefold()

        source_blob = json.dumps(
            storage._read_json(storage.SESSIONS_DIR / sid / "source.json", {}),
            ensure_ascii=False,
        ).casefold()
        assert "если игрок не дал реплику" not in source_blob
        assert "npc действуют самостоятельно" in source_blob

def test_active_character_receives_complete_knowledge_journal_in_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge_journal"] = [
            {"entry_id": f"j{i}", "text": f"fact {i}", "turn": i}
            for i in range(1, 121)
        ]
        storage._write_json(root / "memory.json", memory)

        manifest, context = read_context(sid, "(посмотреть на NPC)")
        assert manifest["chunk_chars_max"] == 16000
        assert manifest["chunk_count"] <= 8
        journal = context["character_memory"]["npc"]["knowledge_journal"]
        assert isinstance(journal, str)
        assert context["character_memory"]["npc"]["knowledge_journal_entry_count"] == 120
        assert "fact 1" in journal
        assert "fact 120" in journal
        assert "entry_id" not in journal

        # Knowledge directing lives in runtime_rules, not another packet rule.
        assert "character_knowledge_rule" not in context
        assert "Частичный факт остаётся частичным." in context["runtime_rules"]



def test_dynamic_relationship_label_can_appear_without_whitelist():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "Остаться рядом.")
        read_all(manifest, sid)

        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Остаться рядом.",
                "scene_output": "Сцена\nОтношения:\nNPC - любовь 2/+2",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "любовь", "value": 2}],
                            "reason": "осознал новое чувство",
                        }
                    ]
                },
            },
        )
        assert result["turn_number"] == 1
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        assert "relationships" not in state
        relationships = storage._read_json(root / "relationships.json", {})
        assert relationships["npc_to_pov"]["npc"]["dimensions"]["любовь"]["value"] == 2

def test_relationship_change_survives_npc_leaving_at_end_of_same_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "Поссориться и разойтись.")
        read_all(manifest, sid)

        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Поссориться и разойтись.",
                "scene_output": "NPC резко отвечает и уходит.",
                "extracted": {
                    "presence_updates": [
                        {"character_id": "npc", "action": "leave"},
                    ],
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "reason": "Ссора перед уходом.",
                            "dimensions": [{"label": "обида", "value": 2}],
                        }
                    ],
                },
            },
        )

        assert result["turn_number"] == 1
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        assert "npc" not in storage._present_character_ids(state)
        relationships = storage._read_json(root / "relationships.json", {})
        assert relationships["npc_to_pov"]["npc"]["dimensions"]["обида"]["value"] == 2


def test_relationship_lens_separates_physical_footer_from_remote_without_hidden_director_prose():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["starting_state"]["relationships"] = {
            "npc": {"интерес": 100},
            "away": {"доверие": 40},
        }
        novel["starting_state"]["current"]["remote_characters"] = ["away"]
        sid = storage.create_session(novel)["session_id"]

        _, context = read_context(sid, "(посмотреть на NPC)")
        lens = context["relationship_lens"]
        assert lens["footer_character_ids"] == ["npc"]
        assert lens["remote_participant_ids"] == ["away"]
        assert "rule" not in lens
        assert "footer_rule" not in lens
        assert "stagnation_rule" not in lens
        assert "Единственный канон отношений - `relationships.json`." in context["runtime_rules"]



def test_saturated_old_metric_does_not_block_new_dynamic_dimension():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["starting_state"]["relationships"] = {"npc": {"интерес": 100}}
        sid = storage.create_session(novel)["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "Сблизиться.")
        read_all(manifest, sid)

        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Сблизиться.",
                "scene_output": "Сцена\nОтношения:\nNPC - интерес 100/0; влечение 3/+3",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "влечение", "value": 3}],
                            "reason": "В сцене возникло качественно новое влечение.",
                        }
                    ]
                },
            },
        )
        root = storage.SESSIONS_DIR / sid
        relationships = storage._read_json(root / "relationships.json", {})
        dims = relationships["npc_to_pov"]["npc"]["dimensions"]
        assert dims["интерес"]["value"] == 100
        assert dims["влечение"]["value"] == 3


def test_new_public_packet_requires_scene_and_persistence_review_before_commit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(посмотреть на NPC)", request_id="writer-review")
        read_all(manifest, sid)

        payload = {
            "packet_id": manifest["packet_id"],
            "user_input": "(посмотреть на NPC)",
            "scene_output": "Сцена продолжается.",
            "extracted": {},
        }
        with pytest.raises(RuntimeError, match="SCENE_BUILDER_REVIEW_REQUIRED"):
            commit_turn_request(sid, payload)

        payload["extracted"] = {
            "scene_builder_reviewed": True,
            "persistence_reviewed": True,
        }
        with pytest.raises(RuntimeError, match="KNOWLEDGE_REVIEW_REQUIRED"):
            commit_turn_request(sid, payload)

        payload["extracted"]["knowledge_reviewed"] = True
        payload["extracted"]["relationship_review"] = [
            {
                "character_id": "npc",
                "changed": False,
                "reason": "Проверено: сцена не изменила отношение NPC к POV.",
            }
        ]
        result = commit_turn_request(sid, payload)
        assert result["turn_number"] == 1


def test_relationship_delta_uses_saved_baseline_and_is_not_double_applied():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["starting_state"]["relationships"] = {"npc": {"доверие": 10}}
        sid = storage.create_session(novel)["session_id"]
        manifest = session_runtime.prepare_turn_packet(sid, "Остаться рядом.")
        read_all(manifest, sid)

        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Остаться рядом.",
                "scene_output": "Сцена\nОтношения:\nNPC - доверие 12/+2",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "доверие", "delta": 2}],
                            "reason": "NPC увидел поступок POV и стал доверять больше",
                        }
                    ]
                },
            },
        )
        root = storage.SESSIONS_DIR / sid
        state = storage._read_json(root / "state.json", {})
        assert "relationships" not in state
        relationships = storage._read_json(root / "relationships.json", {})
        assert relationships["npc_to_pov"]["npc"]["dimensions"]["доверие"]["value"] == 12


def test_pending_packet_has_no_mandatory_relationship_review_and_commits():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(посмотреть на NPC)", request_id="rel-review-lite")
        read_all(manifest, sid)
        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        assert "relationship_review_required" not in packet
        assert manifest["relationship_review_required"] is False
        for key in (
            "relationship_review_details_required",
            "relationship_footer_scope_required",
            "relationship_review_v3_required",
        ):
            assert key not in packet

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на NPC)",
                "scene_output": "Сцена продолжается.",
                "extracted": {
                    "scene_builder_reviewed": True,
                    "persistence_reviewed": True,
                    "knowledge_reviewed": True,
                    "relationship_review": [
                        {
                            "character_id": "npc",
                            "changed": False,
                            "reason": "Сцена проверена: отношение NPC к POV не изменилось.",
                        }
                    ],
                },
            },
        )
        assert result["turn_number"] == 1
        turns = storage._read_turns(root)
        assert "relationship_review" not in turns[-1].get("extracted", {})

def test_explicit_chronology_participants_are_mirrored_into_personal_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "Сказать NPC важный факт.")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "Сказать NPC важный факт.",
                "scene_output": "**POV** — Завтра поезд уходит в шесть.",
                "extracted": {
                    "chronology": [
                        {
                            "event": "NPC узнал, что поезд завтра уходит в шесть.",
                            "knowledge_participants": ["npc"],
                            "importance": "major",
                        }
                    ]
                },
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        npc_text = " ".join(
            row["text"] for row in memory["characters"]["npc"]["knowledge_journal"]
        )
        away_text = " ".join(
            row["text"] for row in memory["characters"]["away"]["knowledge_journal"]
        )
        assert "поезд завтра уходит в шесть" in npc_text
        assert "поезд завтра уходит в шесть" not in away_text

        second = session_runtime.prepare_turn_packet(sid, "(перейти к следующему дню)")
        read_all(second, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": second["packet_id"],
                "user_input": "(перейти к следующему дню)",
                "scene_output": "Наступило следующее утро.",
                "extracted": {"state_patch": {"current": {"date": "02.09.2026"}}},
            },
        )

        _, context = read_context(sid, "(посмотреть на NPC)")
        npc_journal = context["character_memory"]["npc"]["knowledge_journal"]
        assert "поезд завтра уходит в шесть" in npc_journal


def test_chronology_without_explicit_knowledge_participants_does_not_grant_memory():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(подумать о секрете)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(подумать о секрете)",
                "scene_output": "POV сохранила секрет при себе.",
                "extracted": {
                    "chronology": [
                        {
                            "event": "POV решила пока не раскрывать секрет.",
                            "importance": "major",
                        }
                    ]
                },
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        npc_text = " ".join(
            row["text"] for row in memory["characters"]["npc"]["knowledge_journal"]
        )
        assert "решила пока не раскрывать секрет" not in npc_text


def test_safe_chronology_marker_repairs_missing_personal_memory_before_next_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        storage._write_json(
            root / "chronology.json",
            [
                {
                    "event_id": "chrono_t1_1",
                    "turn_number": 1,
                    "story_date": "01.09.2026",
                    "period": "день",
                    "event": "NPC узнал код от сейфа.",
                    "participants_present": ["pov", "npc"],
                    "knowledge_participants": ["npc"],
                    "importance": "major",
                }
            ],
        )

        _, context = read_context(sid, "(посмотреть на NPC)")
        npc_journal = context["character_memory"]["npc"]["knowledge_journal"]
        assert "NPC узнал код от сейфа" in npc_journal

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        assert any(
            "NPC узнал код от сейфа" in row["text"]
            for row in memory["characters"]["npc"]["knowledge_journal"]
        )


def test_exact_duplicate_persisted_journal_rows_are_compacted_before_packet():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge_journal"] = [
            {"entry_id": "a", "date": "01.09.2026", "text": "Рината любит чёрный кофе.", "turn": 1},
            {"entry_id": "b", "date": "01.09.2026", "text": "Рината любит чёрный кофе.", "turn": 2},
            {"entry_id": "c", "date": "01.09.2026", "text": "Рината не любит молоко.", "turn": 3},
        ]
        storage._write_json(root / "memory.json", memory)

        _, context = read_context(sid, "(посмотреть на NPC)")
        persisted = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        rows = persisted["characters"]["npc"]["knowledge_journal"]
        assert len(rows) == 2
        assert context["character_memory"]["npc"]["knowledge_journal_entry_count"] == 2
        assert context["character_memory"]["npc"]["knowledge_journal"].count("Рината любит чёрный кофе.") == 1


def test_repeated_identical_new_fact_is_not_appended_on_later_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        for text in ("Первый раз.", "Второй раз."):
            manifest = session_runtime.prepare_turn_packet(sid, text)
            read_all(manifest, sid)
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": text,
                    "scene_output": text,
                    "extracted": {
                        "knowledge_journal_add": [
                            {"character_id": "npc", "text": "Рината любит чёрный кофе."}
                        ]
                    },
                },
            )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        rows = [
            row for row in memory["characters"]["npc"]["knowledge_journal"]
            if row.get("text") == "Рината любит чёрный кофе."
        ]
        assert len(rows) == 1


def test_chronology_mirror_does_not_duplicate_model_supplied_journal_fact():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "Сказать NPC дату.")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Сказать NPC дату.",
                "scene_output": "**POV** — Встреча третьего сентября.",
                "extracted": {
                    "knowledge_journal_add": [
                        {
                            "character_id": "npc",
                            "text": "Встреча третьего сентября.",
                        }
                    ],
                    "chronology": [
                        {
                            "event": "Встреча третьего сентября.",
                            "participants": ["npc"],
                            "importance": "major",
                        }
                    ],
                },
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        matching = [
            row for row in memory["characters"]["npc"]["knowledge_journal"]
            if "Встреча третьего сентября" in row["text"]
        ]
        assert len(matching) == 1


def test_incomplete_packet_error_precedes_writer_review_gate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        manifest = prepare_turn_request(sid, "(посмотреть на NPC)", request_id="incomplete-before-review")
        assert manifest["chunk_count"] > 1

        payload = {
            "packet_id": manifest["packet_id"],
            "user_input": "(посмотреть на NPC)",
            "scene_output": "Сцена.",
            "extracted": {},
        }
        with pytest.raises(RuntimeError, match="TURN_PACKET_INCOMPLETE"):
            commit_turn_request(sid, payload)

        read_all(manifest, sid)
        with pytest.raises(RuntimeError, match="SCENE_BUILDER_REVIEW_REQUIRED"):
            commit_turn_request(sid, payload)


def test_preexisting_pending_packet_is_not_retroactively_writer_review_gated():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        # Simulates a packet created before the public writer-review flag existed.
        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на NPC)")
        read_all(manifest, sid)
        packet = storage._read_json(storage.SESSIONS_DIR / sid / "turn_packet.json", {})
        assert not packet.get("writer_review_required")

        result = commit_turn_request(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на NPC)",
                "scene_output": "Сцена продолжается.",
                "extracted": {},
            },
        )
        assert result["turn_number"] == 1


def test_generated_remote_message_is_persisted_for_sender_and_pov():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": "**Энже (в сообщениях)** — Живая?",
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        enzhe_text = " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        pov_text = " ".join(
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
        )
        assert "Живая?" in enzhe_text
        assert "Живая?" in pov_text
        assert memory["characters"]["enzhe"]["dialogue_memory"]
        assert memory["characters"]["pov"]["dialogue_memory"]

        second = session_runtime.prepare_turn_packet(sid, "(ответить Энже - живая. чего тебе?)")
        read_all(second, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": second["packet_id"],
                "user_input": "(ответить Энже - живая. чего тебе?)",
                "scene_output": "**Рината** — *(в сообщениях)* живая. чего тебе?",
                "extracted": {
                    "state_patch": {"current": {"date": "02.09.2026"}},
                },
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        enzhe_text = " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        assert "живая. чего тебе?" in enzhe_text
        assert "Живая?" in enzhe_text

        bundle = character_chunk_read._participation_bundle(sid, "enzhe")
        bundle_text = json.dumps(bundle, ensure_ascii=False)
        assert "Живая?" in bundle_text
        assert "живая. чего тебе?" in bundle_text


def test_remote_message_survives_when_sender_enters_physically_later_same_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        novel["starting_state"]["current"]["remote_characters"] = ["enzhe"]
        sid = storage.create_session(novel)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": (
                    "**Энже (в сообщениях)** — Открой дверь.\n"
                    "Через некоторое время Энже вошла в комнату."
                ),
                "extracted": {
                    "presence_updates": [{"character_id": "enzhe", "action": "enter"}],
                    "state_patch": {"current": {"remote_characters": []}},
                },
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        enzhe_text = " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        pov_text = " ".join(
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
        )
        assert "Открой дверь." in enzhe_text
        assert "Открой дверь." in pov_text


def test_autonomous_pov_remote_reply_is_persisted_when_counterpart_is_unambiguous():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        novel["starting_state"]["current"]["remote_characters"] = ["enzhe"]
        sid = storage.create_session(novel)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": (
                    "**Энже (в сообщениях)** — Живая?\n"
                    "**Рината** — *(в сообщениях)* Живая. Чего тебе?"
                ),
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        enzhe_text = " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        pov_text = " ".join(
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
        )
        assert "Живая?" in enzhe_text
        assert "Живая. Чего тебе?" in enzhe_text
        assert "Живая?" in pov_text
        assert "Живая. Чего тебе?" in pov_text


def test_direct_user_remote_reply_is_not_duplicated_by_scene_echo():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        novel["starting_state"]["current"]["remote_characters"] = ["enzhe"]
        sid = storage.create_session(novel)["session_id"]

        user_input = "(ответить Энже - Живая. Чего тебе?)"
        manifest = session_runtime.prepare_turn_packet(sid, user_input)
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": user_input,
                "scene_output": "**Рината** — *(в сообщениях)* Живая. Чего тебе?",
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        enzhe_rows = [
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
            if "Живая. Чего тебе?" in row["text"]
        ]
        pov_rows = [
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
            if "Живая. Чего тебе?" in row["text"]
        ]
        assert len(enzhe_rows) == 1
        assert len(pov_rows) == 1


def test_generated_remote_message_is_redacted_from_shared_recent_history_and_blocked_for_bystander():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
            {"character_id": "dante", "name": "Дантэ"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov", "dante"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": "**Энже (в сообщениях)** — Сайлас мне нравится.",
                "extracted": {},
            },
        )

        second, context = read_context(sid, "(посмотреть на Дантэ)")
        recent_blob = json.dumps(context["recent_turns"], ensure_ascii=False)
        assert "Сайлас мне нравится" not in recent_blob
        assert "содержание приватной коммуникации скрыто" in recent_blob

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        assert "Сайлас мне нравится" in " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        assert "Сайлас мне нравится" in " ".join(
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
        )
        assert "Сайлас мне нравится" not in " ".join(
            row["text"] for row in memory["characters"]["dante"]["knowledge_journal"]
        )

        read_all(second, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": second["packet_id"],
                    "user_input": "(посмотреть на Дантэ)",
                    "scene_output": "**Дантэ** — Энже написала, что Сайлас ей нравится.",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "dante"


def test_generated_remote_message_is_redacted_after_it_moves_to_continuity_tail():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": "**Энже (в сообщениях)** — Секретная фраза для Ринаты.",
                "extracted": {},
            },
        )

        for number in (2, 3):
            user_input = f"(обычный ход {number})"
            manifest = session_runtime.prepare_turn_packet(sid, user_input)
            read_all(manifest, sid)
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": user_input,
                    "scene_output": f"Обычная сцена {number}.",
                    "extracted": {},
                },
            )

        _, context = read_context(sid, "(ещё один ход)")
        continuity = json.dumps(context["continuity_turns"], ensure_ascii=False)
        assert "Секретная фраза для Ринаты" not in continuity
        assert "содержание приватной коммуникации скрыто" in continuity


def test_phone_call_marker_persists_remote_memory_without_preexisting_remote_state():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(взять трубку)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(взять трубку)",
                "scene_output": "**Энже (по телефону)** — Ты где?",
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        assert "Ты где?" in " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        assert "Ты где?" in " ".join(
            row["text"] for row in memory["characters"]["pov"]["knowledge_journal"]
        )


def test_explicit_remote_target_is_not_misattributed_when_two_contacts_are_active():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
            {"character_id": "dante", "name": "Дантэ"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        novel["starting_state"]["current"]["remote_characters"] = ["enzhe", "dante"]
        sid = storage.create_session(novel)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(ответить в чате)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(ответить в чате)",
                "scene_output": "**Рината (в сообщениях Энже)** — Отвечу позже.",
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        assert "Отвечу позже." in " ".join(
            row["text"] for row in memory["characters"]["enzhe"]["knowledge_journal"]
        )
        assert "Отвечу позже." not in " ".join(
            row["text"] for row in memory["characters"]["dante"]["knowledge_journal"]
        )


def test_generated_remote_exchange_is_compacted_to_one_durable_row_per_participant():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        novel["starting_state"]["current"]["remote_characters"] = ["enzhe"]
        sid = storage.create_session(novel)["session_id"]

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на телефон)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(посмотреть на телефон)",
                "scene_output": (
                    "**Энже (в сообщениях)** — Ты живая?\n"
                    "**Энже (в сообщениях)** — Я серьёзно.\n"
                    "**Рината** — *(в сообщениях)* Живая."
                ),
                "extracted": {},
            },
        )

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        assert len(memory["characters"]["enzhe"]["knowledge_journal"]) == 1
        assert len(memory["characters"]["pov"]["knowledge_journal"]) == 1
        assert "Ты живая?" in memory["characters"]["enzhe"]["knowledge_journal"][0]["text"]
        assert "Я серьёзно." in memory["characters"]["enzhe"]["knowledge_journal"][0]["text"]
        assert "Живая." in memory["characters"]["enzhe"]["knowledge_journal"][0]["text"]


def test_continuation_handoff_keeps_generated_remote_message_private():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "enzhe", "name": "Энже"},
            {"character_id": "dante", "name": "Дантэ"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov", "dante"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 0
        meta["continuation_of_session_id"] = "source-session"
        storage._write_json(root / "meta.json", meta)

        bridge = [
            {
                "turn_number": 797,
                "user_input": "(посмотреть на телефон)",
                "scene_output": "**Энже (в сообщениях)** — Только Ринате: кодовое слово маяк.",
                "extracted": {
                    "dialogue_memory_add": [{
                        "topic_id": "remote_t797_enzhe",
                        "participants": ["pov", "enzhe"],
                        "mode": "remote",
                        "summary": "Энже: Только Ринате: кодовое слово маяк.",
                        "segments": [{
                            "speaker_id": "enzhe",
                            "text": "Только Ринате: кодовое слово маяк.",
                        }],
                        "turn": 797,
                    }]
                },
            },
            {
                "turn_number": 798,
                "user_input": "(идти дальше)",
                "scene_output": "Обычная сцена 798.",
                "extracted": {},
            },
            {
                "turn_number": 799,
                "user_input": "(идти дальше)",
                "scene_output": "Обычная сцена 799.",
                "extracted": {},
            },
            {
                "turn_number": 800,
                "user_input": "(остановиться)",
                "scene_output": "Обычная сцена 800.",
                "extracted": {},
            },
        ]
        storage._write_json(root / "handoff_tail.json", bridge)

        manifest, context = read_context(sid, "(посмотреть на Дантэ)")
        continuity_blob = json.dumps(context["continuity_turns"], ensure_ascii=False)
        assert "кодовое слово маяк" not in continuity_blob
        assert "содержание приватной коммуникации скрыто" in continuity_blob

        read_all(manifest, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": "(посмотреть на Дантэ)",
                    "scene_output": "**Дантэ** — Энже написала тебе кодовое слово маяк.",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "dante"


def test_turn_commit_schema_exposes_review_flags_without_exact_scene_format():
    model = TurnCommit(
        packet_id="packet",
        user_input="test",
        scene_output="Короткая тестовая сцена.",
        extracted={},
    )
    assert model.extracted.chronology == []
    assert model.extracted.knowledge_add == []
    assert model.extracted.scene_builder_reviewed is False
    assert model.extracted.persistence_reviewed is False


def test_fifteenth_turn_does_not_create_mandatory_audit_gate():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [{"character_id": "pov", "name": "POV", "is_pov": True}]
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 14
        meta["audit_required"] = False
        storage._write_json(root / "meta.json", meta)

        manifest = session_runtime.prepare_turn_packet(sid, "Ход 15.")
        read_all(manifest, sid)
        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "Ход 15.",
                "scene_output": "Ход пятнадцать.",
                "extracted": {},
            },
        )
        assert result["turn_number"] == 15
        assert result["audit_due"] is False
        assert storage._read_json(root / "meta.json", {})["audit_required"] is False


def test_last_turn_rollback_still_works_without_mandatory_audit():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [{"character_id": "pov", "name": "POV", "is_pov": True}]
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest = session_runtime.prepare_turn_packet(sid, "(подойти к окну)")
        read_all(manifest, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": manifest["packet_id"],
                "user_input": "(подойти к окну)",
                "scene_output": "POV подошла к окну.",
                "extracted": {
                    "state_patch": {
                        "current": {
                            "location": "window",
                            "present_characters": ["pov"],
                        }
                    }
                },
            },
        )
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 1

        rolled = rollback_last_turn(sid, 1, True)
        assert rolled["rolled_back_turn"] == 1
        assert rolled["turn_number"] == 0
        assert storage._read_json(root / "meta.json", {})["turn_number"] == 0
        assert storage._read_turns(root) == []
        assert storage._read_json(root / "state.json", {})["current"]["location"] == "room"


def test_v5_setup_preserves_and_seeds_starting_character_knowledge():
    draft = {
        "title": "Start Knowledge",
        "novel_id": "start-knowledge",
        "version": 5,
        "sections": {
            "novel": {"pov_character": "pov"},
            "characters": [
                {"character_id": "pov", "name": "POV", "is_pov": True},
                {"character_id": "npc", "name": "NPC"},
            ],
            "hidden_lore": {},
            "knowledge": {
                "pov": [
                    {"text": "До первой сцены читала досье NPC и знает, что ему 27 лет."}
                ]
            },
        },
    }
    normalized_knowledge = simple_setup_runtime._normalise_simple_section(
        draft,
        "knowledge",
        draft["sections"]["knowledge"],
    )
    assert normalized_knowledge["pov"][0]["text"].endswith("27 лет.")
    draft["sections"]["knowledge"] = normalized_knowledge

    template = simple_setup_runtime._content_template(draft)
    assert template["knowledge"]["pov"][0]["text"].endswith("27 лет.")
    template, _ = simple_setup_runtime._validate_simple_content(template)
    assert template["knowledge"]["pov"][0]["text"].endswith("27 лет.")

    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(template)["session_id"]
        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        journal = memory["characters"]["pov"]["knowledge_journal"]
        assert len(journal) == 1
        assert journal[0]["turn"] == 0
        assert "27 лет" in journal[0]["text"]
        assert memory["characters"]["npc"]["knowledge_journal"] == []


def test_v5_offscreen_bundle_contains_own_complete_knowledge_without_second_read():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "away")
        bucket["knowledge_journal"] = [
            {"entry_id": f"k{i}", "text": f"away fact {i}", "turn": i}
            for i in range(1, 121)
        ]
        storage._write_json(root / "memory.json", memory)

        bundle = character_chunk_read._participation_bundle(sid, "away")
        assert bundle["knowledge_complete"] is True
        assert len(bundle["knowledge_journal"]) == 120
        assert bundle["knowledge_journal"][0]["text"] == "away fact 1"
        assert bundle["knowledge_journal"][-1]["text"] == "away fact 120"
        assert "knowledge_source" not in bundle
        assert "prepareCharacterKnowledgeRead" not in str(bundle)
        assert bundle["knowledge_scope"]["own_card_is_self_known_except_explicit_hidden_branches"] is True
    assert "unknown_to_self" in str(bundle["knowledge_scope"])


def test_opening_scene_uses_empty_gameplay_input_not_service_command():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        manifest = prepare_turn_request(
            sid,
            "запускай первую сцену",
            request_id="opening-1",
            opening_scene=True,
        )
        assert manifest["opening_scene"] is True
        root = storage.SESSIONS_DIR / sid
        packet = storage._read_json(root / "turn_packet.json", {})
        assert packet["user_input"] == ""

        pieces = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            pieces.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
        context = json.loads("".join(pieces))
        assert context["opening_scene"]["active"] is True
        assert context["player_input_map"]["spoken_segments"] == []
        assert context["player_input_map"]["ordered_segments"] == []

        retry = prepare_turn_request(
            sid,
            "запускай первую сцену",
            request_id="opening-1",
            opening_scene=True,
        )
        assert retry["packet_id"] == manifest["packet_id"]
        assert retry["opening_scene"] is True
        assert retry["reused_pending_packet"] is True



def test_secondary_cast_cues_are_bounded_and_readable_before_selection():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["novel"]["core_cast"] = [{"character_id": "pov", "story_function": "POV"}]
        novel["characters"] = [novel["characters"][0]] + [
            {
                "character_id": f"support_{i}", "name": f"Support {i}",
                "story_function": "коллега", "work": "учитель " + "W" * 500,
                "habits": "заходит к коллегам " + "H" * 500,
                "personality": "общительный " + "C" * 500,
            }
            for i in range(52)
        ]
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]
        _, context = read_context(sid, "(подождать)")
        rows = context["cast_registry"]["characters"]
        assert {row["character_id"] for row in rows} == {"pov", *[f"support_{i}" for i in range(52)]}
        assert [card["character_id"] for card in context["character_cards"]] == ["pov"]
        for row in rows:
            if row["character_id"] == "pov":
                continue
            assert row["importance"] == "support"
            assert row["work"].startswith("учитель") and len(row["work"]) <= 220
            assert row["habits"].startswith("заходит к коллегам") and len(row["habits"]) <= 260
            assert row["character"].startswith("общительный") and len(row["character"]) <= 260
            assert "active_intents" not in row and "active_threads" not in row

        # An exploratory read gives the full card and does not put the NPC in the scene.
        manifest = character_chunk_read.prepare_character_bundle_read(sid, "support_0")
        parts = [manifest["content"]]
        for index in range(1, manifest["chunk_count"]):
            parts.append(character_chunk_read.get_character_bundle_chunk(
                sid, "support_0", manifest["read_id"], index,
            )["content"])
        bundle = json.loads("".join(parts))
        assert "W" * 500 in bundle["profile"]
        assert "C" * 500 in bundle["profile"]
        saved = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert saved["current"]["present_characters"] == ["pov"]


def test_cast_registry_exposes_causal_character_data_without_recency_pressure():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"][1]["work"] = "инструктор"
        novel["characters"][1]["habits"] = "после обеда заходит к коллегам"
        novel["characters"][1]["character"] = "общительный, вспыльчивый"
        novel["characters"][1]["residence"] = "база"
        novel["characters"][1]["relationships"] = [
            {
                "target_character_id": "away",
                "relationship_type": "давно знакомы",
            }
        ]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "Остаться рядом.")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "Остаться рядом.",
                "scene_output": "Сцена один.\nОтношения:\nNPC - близость 1/+1",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "близость", "value": 1}],
                            "reason": "впервые сознательно остался рядом с POV",
                        }
                    ],
                    "npc_intent_updates": [
                        {
                            "character_id": "npc",
                            "intent_id": "ask_again",
                            "status": "active",
                            "summary": "вернуться к незакрытому вопросу",
                        }
                    ],
                },
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(проводить NPC)")
        read_all(second, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": second["packet_id"],
                "user_input": "(проводить NPC)",
                "scene_output": "NPC ушёл.",
                "extracted": {
                    "presence_updates": [{"character_id": "npc", "action": "leave"}]
                },
            },
        )

        _, context = read_context(sid, "(остаться одной)")
        registry = context["cast_registry"]
        row = next(x for x in registry["characters"] if x["character_id"] == "npc")

        assert registry["mandatory_causal_review"] is True
        assert "return_pressure" in registry
        assert registry["important_cast_return_required"] is bool(registry["return_pressure"])
        assert "Режиссура сама находит логичный способ сталкивать важный каст" in registry["instruction"]
        assert row["story_function"] == "possible romance"
        assert "добиться ответа" in row["goals"]
        assert "initiative_cues" not in row
        assert row["work"] == "инструктор"
        assert row["habits"] == "после обеда заходит к коллегам"
        assert row["character"] == "общительный, вспыльчивый"
        assert "residence" not in row
        assert row["pov_relationship"]["близость"] == 1
        assert "known_relationships" not in row
        assert "npc_relationships" not in row
        assert any(
            ref.get("other_character_id") == "away"
            for ref in row.get("npc_relation_refs", [])
        )
        assert "вернуться к незакрытому вопросу" in row["active_intents"]
        assert "остался рядом" in row["last_meaningful_event"]

        for forbidden in (
            "last_physical_turn",
            "last_physical_game_day",
            "turns_since_physical",
            "game_days_since_physical",
            "overdue",
            "last_seen",
        ):
            assert forbidden not in row

def test_offscreen_npc_without_saved_intent_can_still_be_reviewed_for_ordinary_self_initiative():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"][1]["work"] = "тренер"
        novel["characters"][1]["habits"] = ["шлёт друзьям мемы", "пишет вечером после работы"]
        novel["characters"][1]["relationships"] = [
            {
                "target_character_id": "pov",
                "relationship_type": "близкий друг",
                "dimensions": [{"label": "привязанность", "value": 65}],
            }
        ]
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        _, context = read_context(sid, "(заняться своими делами)")
        assert "offscreen_intent_candidates" not in context
        row = next(
            item for item in context["cast_registry"]["characters"]
            if item["character_id"] == "npc"
        )
        assert row.get("active_intents") in (None, [])
        assert row.get("active_threads") in (None, [])
        assert "initiative_cues" not in row
        assert row["work"] == "тренер"
        assert "пишет вечером после работы" in row["habits"]
        assert row["pov_relationship"]["привязанность"] == 65
        assert "Личной мотивации NPC, его отношения, цели или story_function достаточно для инициативы." in context["runtime_rules"]
        assert "Крупный сюжетный триггер не требуется." in context["runtime_rules"]
        assert "до реального участия прочитай его целиком" in context["cast_registry"]["instruction"]


def test_legacy_pending_packet_is_refreshed_into_current_knowledge_context():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        bucket = storage._memory_bucket(memory, "npc")
        bucket["knowledge_journal"] = [
            {"entry_id": "old-1", "text": "NPC уже знает этот факт.", "turn": 0}
        ]
        storage._write_json(root / "memory.json", memory)

        manifest = session_runtime.prepare_turn_packet(sid, "(посмотреть на NPC)")
        packet = storage._read_json(root / "turn_packet.json", {})
        raw = "".join(packet["chunks"])
        context = json.loads(raw)
        context.pop("character_memory", None)
        context["simple_knowledge_rules"] = {
            "knowledge_transport": "prepareCharacterKnowledgeRead + getCharacterKnowledgeChunk"
        }
        text_payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        packet["chunks"] = [text_payload]
        packet["chunk_count"] = 1
        packet["read_chunks"] = [0]
        packet["turn_pipeline_version"] = 3
        storage._write_json(root / "turn_packet.json", packet)

        old_packet_id = packet["packet_id"]
        refreshed = session_runtime.prepare_turn_packet(sid, "(посмотреть на NPC)")
        assert refreshed["reused_pending_packet"] is False
        assert refreshed["packet_id"] != old_packet_id

        rebuilt = json.loads(
            "".join(storage._read_json(root / "turn_packet.json", {})["chunks"])
        )
        assert "simple_knowledge_rules" not in rebuilt
        assert rebuilt["character_memory"]["npc"]["knowledge_journal"] == "NPC уже знает этот факт."
        assert rebuilt["character_memory"]["npc"]["knowledge_journal_entry_count"] == 1
        assert "working_context_contract" not in rebuilt



def test_rules_keep_director_truth_separate_from_character_truth():
    docs = runtime_access.runtime_documents()
    rules = docs["rules"]
    builder = docs["scene_builder"]

    for phrase in (
        "chronology",
        "hidden_lore",
        "future_guidance",
        "director-only",
    ):
        assert phrase in rules
    assert "авторский план" in builder
    assert "Источник знания должен существовать до использования знания." in rules

    for removed_belief_rule in (
        "NPC не обязан автоматически верить POV",
        "не обязан автоматически ему не верить",
        "Оценивай правдоподобие слов POV",
        "NPC не получает авторское чувство лжи",
        "Если Элен сказала NPC «я не рейдер»",
        "не все должны верить в доброту",
    ):
        assert removed_belief_rule not in rules

def test_continuation_keeps_character_knowledge_separate_in_v5():
    source = {"version": 5, "profile_schema": {"version": 1}}
    cards = [
        {"character_id": "pov", "name": "POV", "is_pov": True},
        {"character_id": "npc", "name": "NPC"},
    ]
    package = {
        "characters": {
            "pov": {"knowledge": ["POV знает только A."]},
            "npc": {"knowledge": ["NPC знает только B."]},
        }
    }

    memory = continuation_runtime._normalized_compact_memory(source, cards, package)
    pov = memory["characters"]["pov"]["knowledge_journal"]
    npc = memory["characters"]["npc"]["knowledge_journal"]

    assert [row["text"] for row in pov] == ["POV знает только A."]
    assert [row["text"] for row in npc] == ["NPC знает только B."]
    assert "B" not in pov[0]["text"]
    assert "A" not in npc[0]["text"]


def test_private_communication_parser_resolves_inflected_russian_recipient():
    cards = [
        {"character_id": "pov", "name": "Рината", "is_pov": True},
        {"character_id": "npc", "name": "Дантэ"},
        {"character_id": "away", "name": "Эдриан"},
    ]
    rows = private_knowledge_runtime.extract_private_communications(
        "(ответить Эдриану - Завтра вернусь домой. продолжать гладить Дантэ)",
        cards,
        turn_number=1,
    )
    assert len(rows) == 1
    assert rows[0]["recipient_id"] == "away"
    assert "Завтра вернусь домой" in rows[0]["payload"]


def test_private_communication_parser_resolves_short_inflected_russian_names():
    cards = [
        {"character_id": "pov", "name": "Эмили", "is_pov": True},
        {"character_id": "ren", "name": "Рен"},
        {"character_id": "chloe", "name": "Хлоя"},
    ]
    ren_rows = private_knowledge_runtime.extract_private_communications(
        "(ответить Рену - Буду позже.)",
        cards,
        turn_number=1,
    )
    chloe_rows = private_knowledge_runtime.extract_private_communications(
        "(переслать Хлое выбранные сообщения Рена)",
        cards,
        turn_number=2,
    )
    assert len(ren_rows) == 1
    assert ren_rows[0]["recipient_id"] == "ren"
    assert len(chloe_rows) == 1
    assert chloe_rows[0]["recipient_id"] == "chloe"


def test_private_message_is_redacted_from_shared_recent_history_and_saved_to_participants():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "npc", "name": "Дантэ"},
            {"character_id": "away", "name": "Эдриан"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov", "npc"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Эдриану - Завтра вернусь домой. продолжать гладить Дантэ)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Эдриану - Завтра вернусь домой. продолжать гладить Дантэ)",
                "scene_output": "**Рината** — *(в сообщении Эдриану)* Завтра вернусь домой.\nДантэ продолжал спать.",
                "extracted": {},
            },
        )

        root = storage.SESSIONS_DIR / sid
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        pov_text = " ".join(row["text"] for row in memory["characters"]["pov"]["knowledge_journal"])
        away_text = " ".join(row["text"] for row in memory["characters"]["away"]["knowledge_journal"])
        npc_text = " ".join(row["text"] for row in memory["characters"]["npc"]["knowledge_journal"])
        assert "Завтра вернусь домой" in pov_text
        assert "Завтра вернусь домой" in away_text
        assert "Завтра вернусь домой" not in npc_text

        _, context = read_context(sid, "(проснуться)")
        recent_blob = json.dumps(context["recent_turns"], ensure_ascii=False)
        assert "Завтра вернусь домой" not in recent_blob
        assert "содержание приватной коммуникации скрыто" in recent_blob


def test_sleeping_bystander_cannot_use_prior_private_message_content():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Рината", "is_pov": True},
            {"character_id": "npc", "name": "Дантэ"},
            {"character_id": "away", "name": "Эдриан"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov", "npc"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Эдриану - Завтра вернусь домой. продолжать гладить Дантэ)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Эдриану - Завтра вернусь домой. продолжать гладить Дантэ)",
                "scene_output": "**Рината** — *(в сообщении Эдриану)* Завтра вернусь домой.\nДантэ продолжал спать.",
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(проснуться)")
        read_all(second, sid)

        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": second["packet_id"],
                    "user_input": "(проснуться)",
                    "scene_output": "**Дантэ** — Ты сегодня сказала Эдриану, что завтра вернёшься домой.",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "npc"


def test_explicit_forward_grants_only_persisted_private_content_to_recipient_same_turn():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Рену - Встречаемся в восемь у старого моста. Я буду на мотоцикле.)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Рену - Встречаемся в восемь у старого моста. Я буду на мотоцикле.)",
                "scene_output": (
                    "**Эмили** — *(в сообщении Рену)* "
                    "Встречаемся в восемь у старого моста. Я буду на мотоцикле."
                ),
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(
            sid,
            "(переслать Хлое выбранные сообщения Рена)",
        )
        read_all(second, sid)
        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": second["packet_id"],
                "user_input": "(переслать Хлое выбранные сообщения Рена)",
                "scene_output": (
                    "**Хлоя** — Значит, встреча в восемь у старого моста? "
                    "И там будет мотоцикл?"
                ),
                "extracted": {
                    "knowledge_journal_add": [{
                        "character_id": "chloe",
                        "text": (
                            "Эмили переслала Хлое выбранные сообщения Рена: "
                            "встреча в восемь у старого моста; упомянут мотоцикл."
                        ),
                    }]
                },
            },
        )
        assert result["turn_number"] == 2

        memory = storage._normalise_memory(
            storage._read_json(storage.SESSIONS_DIR / sid / "memory.json", {})
        )
        chloe_text = " ".join(
            row["text"] for row in memory["characters"]["chloe"]["knowledge_journal"]
        )
        assert "восемь" in chloe_text
        assert "мотоцикл" in chloe_text

        third = session_runtime.prepare_turn_packet(sid, "(позвонить Хлое)")
        read_all(third, sid)
        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": third["packet_id"],
                "user_input": "(позвонить Хлое)",
                "scene_output": "**Хлоя** — В восемь у старого моста, я помню.",
                "extracted": {},
            },
        )
        assert result["turn_number"] == 3


def test_forwarding_one_private_source_does_not_unlock_another_private_source():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "ethan", "name": "Итан"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        for user_input, scene_output in (
            (
                "(ответить Рену - Встречаемся у старого моста. Я буду на мотоцикле.)",
                "**Эмили** — *(в сообщении Рену)* Встречаемся у старого моста. Я буду на мотоцикле.",
            ),
            (
                "(ответить Итану - Пароль от сейфа апельсиновый.)",
                "**Эмили** — *(в сообщении Итану)* Пароль от сейфа апельсиновый.",
            ),
        ):
            manifest = session_runtime.prepare_turn_packet(sid, user_input)
            read_all(manifest, sid)
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": user_input,
                    "scene_output": scene_output,
                    "extracted": {},
                },
            )

        manifest = session_runtime.prepare_turn_packet(
            sid,
            "(переслать Хлое выбранные сообщения Рена)",
        )
        read_all(manifest, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": manifest["packet_id"],
                    "user_input": "(переслать Хлое выбранные сообщения Рена)",
                    "scene_output": "**Хлоя** — А пароль от сейфа всё ещё апельсиновый?",
                    "extracted": {
                        "knowledge_journal_add": [{
                            "character_id": "chloe",
                            "text": "Эмили переслала Хлое сообщения Рена про старый мост и мотоцикл.",
                        }]
                    },
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "chloe"


def test_ordinary_message_to_recipient_does_not_unlock_third_party_private_history():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Рену - Кодовое слово мандариновый.)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Рену - Кодовое слово мандариновый.)",
                "scene_output": "**Эмили** — *(в сообщении Рену)* Кодовое слово мандариновый.",
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(написать Хлое - привет)")
        read_all(second, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": second["packet_id"],
                    "user_input": "(написать Хлое - привет)",
                    "scene_output": "**Хлоя** — А кодовое слово всё ещё мандариновый?",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "chloe"


def test_single_ordinary_word_overlap_does_not_trigger_private_leak():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Рену - Сегодня здравый смысл победил усталость.)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Рену - Сегодня здравый смысл победил усталость.)",
                "scene_output": (
                    "**Эмили** — *(в сообщении Рену)* "
                    "Сегодня здравый смысл победил усталость."
                ),
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(поговорить с Хлоей)")
        read_all(second, sid)
        result = session_runtime.commit_turn(
            sid,
            {
                "packet_id": second["packet_id"],
                "user_input": "(поговорить с Хлоей)",
                "scene_output": "**Хлоя** — Тогда понятно. Сильнейший инстинкт победил.",
                "extracted": {},
            },
        )
        assert result["turn_number"] == 2


def test_two_private_content_terms_still_trigger_leak():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Рену - Старый мост и мотоцикл.)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Рену - Старый мост и мотоцикл.)",
                "scene_output": "**Эмили** — *(в сообщении Рену)* Старый мост и мотоцикл.",
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(поговорить с Хлоей)")
        read_all(second, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": second["packet_id"],
                    "user_input": "(поговорить с Хлоей)",
                    "scene_output": "**Хлоя** — Старый мост и мотоцикл, значит?",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "chloe"


def test_explicit_one_word_code_still_triggers_private_leak():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        novel = base_novel()
        novel["characters"] = [
            {"character_id": "pov", "name": "Эмили", "is_pov": True},
            {"character_id": "ren", "name": "Рен"},
            {"character_id": "chloe", "name": "Хлоя"},
        ]
        novel["starting_state"]["pov"] = {"character_id": "pov"}
        novel["starting_state"]["current"]["present_characters"] = ["pov"]
        sid = storage.create_session(novel)["session_id"]

        first = session_runtime.prepare_turn_packet(
            sid,
            "(ответить Рену - Кодовое слово мандариновый.)",
        )
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(ответить Рену - Кодовое слово мандариновый.)",
                "scene_output": "**Эмили** — *(в сообщении Рену)* Кодовое слово мандариновый.",
                "extracted": {},
            },
        )

        second = session_runtime.prepare_turn_packet(sid, "(поговорить с Хлоей)")
        read_all(second, sid)
        with pytest.raises(Exception) as exc:
            session_runtime.commit_turn(
                sid,
                {
                    "packet_id": second["packet_id"],
                    "user_input": "(поговорить с Хлоей)",
                    "scene_output": "**Хлоя** — Мандариновый.",
                    "extracted": {},
                },
            )
        detail = getattr(exc.value, "detail", {})
        assert isinstance(detail, dict)
        assert detail.get("code") == "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK"
        assert detail.get("character_id") == "chloe"


def test_present_character_remains_present_without_leave():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        first = session_runtime.prepare_turn_packet(sid, "(посмотреть на NPC)")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "(посмотреть на NPC)",
                "scene_output": "**NPC** — Ты мне ответишь?",
                "extracted": {},
            },
        )
        _, context = read_context(sid, "Да. Отвечу.")
        assert "npc" in context["scene_presence"]["present_character_ids"]
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert "npc" in state["current"]["present_characters"]


def test_active_writer_packet_exposes_return_pressure_for_long_absent_core_cast():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        state = storage._read_json(root / "state.json", {})
        state["current"]["present_characters"] = ["pov"]
        state["world"] = {
            "cast_registry": {
                "npc": {
                    "character_id": "npc",
                    "name": "NPC",
                    "origin": "player_created",
                    "importance": "core",
                    "first_registered_turn": 0,
                    "last_appearance_turn": 1,
                    "last_contact_turn": 1,
                    "appearance_count": 2,
                    "status": "active",
                }
            }
        }
        storage._write_json(root / "state.json", state)

        turns = [
            {
                "turn_number": number,
                "user_input": "",
                "scene_output": "",
                "extracted": {},
            }
            for number in range(1, 26)
        ]
        (root / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in turns),
            encoding="utf-8",
        )
        meta = storage._read_json(root / "meta.json", {})
        meta["turn_number"] = 25
        storage._write_json(root / "meta.json", meta)

        _, context = read_context(sid, "(заняться своими делами)")
        registry = context["cast_registry"]
        pressured = {row["character_id"] for row in registry["return_pressure"]}

        assert registry["important_cast_return_required"] is True
        assert "npc" in pressured
        assert "ближайшему логичному контакту" in registry["instruction"]
