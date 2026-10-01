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
    assert "выбор информации всегда остаётся игроку" in docs["rules"]
    assert "Источник знания должен существовать ДО" in docs["rules"]
    assert "Формат scene_builder обязателен" in docs["scene_builder"]
    assert "ТОЧКА ОСТАНОВКИ СЦЕНЫ" in docs["scene_builder"]
    assert "Если такой точки ещё нет — сцена ещё не закончена." in docs["scene_builder"]
    assert "Не придумывай ради этого обязательный звонок" not in docs["scene_builder"]
    assert "Если такой точки ещё нет — сцена ещё не закончена." in docs["scene_builder"]
    assert "Варианты появляются только ПОСЛЕ" in docs["scene_builder"]
    assert "НЕ ОПИСЫВАЙ НЕСЛУЧИВШЕЕСЯ" in docs["scene_builder"]
    assert "Отсутствие действия само по себе не является событием" in docs["scene_builder"]
    assert "Не перечисляй отсутствующие действия" in docs["scene_builder"]
    assert "Конец сцены и варианты опираются только на события" in docs["rules"]
    assert "POV не превращается в молчащую камеру" in docs["rules"]
    assert "не обязан ждать нового хода игрока после каждой реплики" in docs["rules"]
    assert "НЕ задают новый режим молчания" in docs["rules"]
    assert "Не продолжай молчание только потому, что POV молчал в последних сценах" in docs["rules"]
    assert "Останавливай POV только перед новым значимым выбором" in docs["rules"]
    assert "не ставь мир на паузу ради хода игрока" in docs["rules"]
    assert "Быт не должен замораживать мир" in docs["rules"]
    assert "present_characters" in docs["rules"]
    assert "state.characters[ID]" in docs["rules"]
    assert "не жди, пока POV специально пойдёт его искать или назовёт" in docs["rules"]
    assert "упоминание также НЕ запрещает его участие" in docs["rules"]
    assert "Простое упоминание отсутствующего персонажа не означает, что нужно читать его карточку" not in docs["rules"]
    assert "Не проматывай через ожидаемого персонажа" in docs["rules"]
    assert "ROUTINE снижает плотность визуального покрытия" in docs["scene_builder"]
    assert "Отношения — часть причин поведения NPC" in docs["rules"]
    assert "Содержание сцены не шаблонизируй" in docs["scene_builder"]
    assert "Не объясняй смысл реплик, поведения, эмоций и отношений" in docs["scene_builder"]
    assert "Последние абзацы сцены должны не закрывать движение" in docs["scene_builder"]
    assert "Не придумывай новый активный элемент только ради эффекта финала" in docs["scene_builder"]
    assert "2000–3000 непробельных символов" in docs["scene_builder"]
    assert "не должна превращаться в чистую стенограмму" in docs["scene_builder"]
    assert "Любую сцену должно быть возможно визуально собрать в голове" in docs["scene_builder"]
    assert "ПРИСУТСТВИЕ И ФИЗИЧЕСКАЯ НЕПРЕРЫВНОСТЬ" in docs["rules"]
    assert "Молчание не удаляет NPC" in docs["rules"]
    assert "Между двумя ходами нет скрытого монтажа" in docs["rules"]
    assert "Начало нового хода физически продолжает конец предыдущего" in docs["scene_builder"]
    assert "Центральный beat важной интимной сцены нельзя заменять" in docs["scene_builder"]
    assert "снижай графичность, а не непрерывность" in docs["scene_builder"]
    assert "Авторский комментарий не заменяет саму сцену" in docs["scene_builder"]
    assert "Плохой монтаж:" not in docs["scene_builder"]
    assert "Плохие варианты:" not in docs["scene_builder"]
    assert "Примеры:" not in docs["scene_builder"]
    assert "unknown_to_self" in docs["rules"]
    assert "короткое содержательное действие" in docs["rules"]
    assert "Слухи, сообщения и чужие знания распространяются только через реальные каналы" in docs["rules"]
    assert "Стартовая анкета, hidden_lore, правила новеллы и открытые сюжетные линии не декорация" in docs["rules"]


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
        ):
            assert removed not in context

        assert list(context)[-2:] == ["runtime_rules", "scene_builder"]
        active = {row["character_id"] for row in context["character_cards"]}
        assert active == {"pov", "npc"}
        assert "away" not in active
        assert context["working_context_contract"]["hidden_director_guard_layers"] is False
        assert context["working_context_contract"]["backend_semantic_scene_gates"] is False
        assert context["working_context_contract"]["precommit_review_gates"] == ["scene_builder", "persistence", "knowledge"]


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
                "scene_output": "Сцена\nОтношения:\nNPC - любовь 12/+2",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "любовь", "value": 12, "delta": 2}],
                            "reason": "осознал чувство",
                        }
                    ]
                },
            },
        )
        assert result["turn_number"] == 1
        state = storage._read_json(storage.SESSIONS_DIR / sid / "state.json", {})
        assert state["relationships"]["npc"]["любовь"] == 12


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
        result = commit_turn_request(sid, payload)
        assert result["turn_number"] == 1


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


def test_cast_registry_exposes_physical_contact_and_meaningful_recency_separately():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        sid = storage.create_session(base_novel())["session_id"]

        first = session_runtime.prepare_turn_packet(sid, "Остаться рядом.")
        read_all(first, sid)
        session_runtime.commit_turn(
            sid,
            {
                "packet_id": first["packet_id"],
                "user_input": "Остаться рядом.",
                "scene_output": "Сцена один.\nОтношения:\nNPC - близость 8/+1",
                "extracted": {
                    "relationship_updates": [
                        {
                            "character_id": "npc",
                            "dimensions": [{"label": "близость", "value": 8, "delta": 1}],
                            "reason": "впервые сознательно остался рядом с POV",
                        }
                    ]
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
        row = next(x for x in context["cast_registry"]["characters"] if x["character_id"] == "npc")
        assert row["last_physical_turn"] == 1
        assert row["last_meaningful_turn"] == 1
        assert row["turns_since_physical"] == 1
        assert row["turns_since_meaningful"] == 1
        assert "остался рядом" in row["last_meaningful_event"]


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

        refreshed = session_runtime.prepare_turn_packet(sid, "(посмотреть на NPC)")
        assert refreshed["reused_pending_packet"] is True

        rebuilt = json.loads(
            "".join(storage._read_json(root / "turn_packet.json", {})["chunks"])
        )
        assert "simple_knowledge_rules" not in rebuilt
        assert rebuilt["character_memory"]["npc"]["knowledge_journal"] == "NPC уже знает этот факт."
        assert rebuilt["character_memory"]["npc"]["knowledge_journal_entry_count"] == 1
        assert rebuilt["working_context_contract"]["active_character_knowledge_rebuilt_from_persistent_memory"] is True


def test_rules_keep_director_truth_separate_from_character_truth():
    docs = runtime_access.runtime_documents()
    rules = docs["rules"]
    for phrase in (
        "chronology",
        "hidden_lore",
        "future_guidance",
        "авторский план",
        "Источник знания должен существовать ДО",
        "Ошибочное мнение или убеждение персонажа не исправляется само",
        "NPC не обязан автоматически верить POV",
        "не обязан автоматически ему не верить",
        "это само по себе НЕ делает NPC подозрительным",
    ):
        assert phrase in rules


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
