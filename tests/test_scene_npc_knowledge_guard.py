import json
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import character_chunk_read, private_knowledge_runtime, scene_knowledge_guard, session_runtime, storage


def _setup(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _novel():
    return {
        "novel_id": "scene-npc-knowledge-guard",
        "title": "Scene NPC Knowledge Guard",
        "version": 5,
        "profile_schema": {"version": 1},
        "novel": {"pov_character": "kair"},
        "characters": [
            {"character_id": "kair", "name": "Кайр", "is_pov": True},
            {"character_id": "mira", "name": "Мира", "role": "major"},
            {"character_id": "adrian", "name": "Адриан", "role": "major"},
        ],
        "starting_state": {
            "pov": {"character_id": "kair"},
            "current": {
                "date": "2026-10-07",
                "time": "22:00",
                "location": "квартира Миры",
                "present_characters": ["kair", "mira", "adrian"],
                "remote_characters": [],
                "remote_channels": {},
            },
        },
    }


def _seed(root, character_id: str, *facts: str) -> None:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = storage._memory_bucket(memory, character_id)
    bucket["knowledge_journal"] = [
        {"entry_id": f"{character_id}_{index}", "text": fact}
        for index, fact in enumerate(facts, start=1)
    ]
    storage._write_json(root / "memory.json", memory)


def _read_packet(manifest, sid: str):
    chunks = [manifest["content"]]
    for index in range(1, manifest["chunk_count"]):
        chunks.append(storage.get_turn_packet_chunk(sid, manifest["packet_id"], index)["content"])
    return json.loads("".join(chunks))


def _prepare(sid: str, user_input: str):
    manifest = session_runtime.prepare_turn_packet(sid, user_input)
    context = _read_packet(manifest, sid)
    return manifest, context


def _rewrite_pending_context(root, context: dict) -> None:
    packet = storage._read_json(root / "turn_packet.json", {})
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    packet["chunks"] = [text]
    packet["chunk_count"] = 1
    packet["read_chunks"] = [0]
    storage._write_json(root / "turn_packet.json", packet)


def _validate(sid: str, manifest: dict, user_input: str, scene_output: str, extracted=None):
    scene_knowledge_guard.validate_scene_output(
        sid,
        {
            "packet_id": manifest["packet_id"],
            "user_input": user_input,
            "scene_output": scene_output,
            "extracted": extracted or {},
        },
    )


def _payload(packet_id: str, user_input: str, scene_output: str):
    return {
        "packet_id": packet_id,
        "user_input": user_input,
        "scene_output": scene_output,
        "extracted": {},
    }



def test_repeated_npc_speech_reuses_knowledge_guard_cache(monkeypatch):
    # Each NPC's stable knowledge sources must be prepared only once per turn,
    # even when that NPC speaks several times.
    calls = {"base": {}, "perception": {}, "protected": {}}
    for label, name in (
        ("base", "_authorized_base_text"),
        ("perception", "_physical_perception_text"),
        ("protected", "_protected_rows"),
    ):
        original = getattr(scene_knowledge_guard, name)

        def track(*args, _label=label, _original=original, **kwargs):
            cid = str(args[1])
            bucket = calls[_label]
            bucket[cid] = bucket.get(cid, 0) + 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(scene_knowledge_guard, name, track)

    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        manifest, _ = _prepare(sid, "(слушать разговор)")
        scene = (
            "**Адриан** — Привет, Кайр.\n\n"
            "**Мира** — Я здесь.\n\n"
            "**Адриан** — Как дела?\n\n"
            "**Мира** — Слушаю.\n\n"
            "**Адриан** — Тогда продолжим."
        )
        _validate(sid, manifest, "(слушать разговор)", scene)

    # Three lines from Adrian and two from Mira must be checked independently,
    # yet each NPC's immutable source texts are built only once per turn.
    for label in calls:
        assert calls[label] == {"adrian": 1, "mira": 1}, (label, calls[label])


def test_personal_experiences_and_dialogue_are_valid_recollection_context():
    bucket = {
        "knowledge_journal": [{"text": "Вар столкнулся с существом."}],
        "experiences": [{"event_id": "meeting", "summary": "Вар пытался удержать существо."}],
        "dialogue_memory": [{
            "topic_id": "meeting_story", "participants": ["var", "kair"],
            "question": "Что ты сделал тогда?",
            "answer": "Я пытался его удержать.",
        }],
    }
    recalled = scene_knowledge_guard._memory_texts_from_bucket(bucket)
    assert "Вар столкнулся с существом." in recalled
    assert "Вар пытался удержать существо." in recalled
    assert "Я пытался его удержать." in recalled


def test_var_can_expand_own_encounter_without_bag_of_words_leak():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["characters"].append({"character_id": "var", "name": "Вар", "role": "major"})
        novel["starting_state"]["current"]["present_characters"].append("var")
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "var", "Вар лично столкнулся с неизвестным существом.")

        manifest, context = _prepare(sid, "(слушать Вара)")
        context = deepcopy(context)
        context["chronology_recent"] = [
            {"summary": "Вар схватил существо. Руки существа остались на месте, пальцы зацепились за край прохода."}
        ]
        _rewrite_pending_context(root, context)

        # The words "hands", "fingers" and "grabbed" also occur in chronology,
        # but this is Var's own plausible firsthand recollection, not a copied secret.
        _validate(
            sid, manifest, "(слушать Вара)",
            "**Вар** — Оно полезло туда, где даже пальцы не просунешь. "
            "Я схватил его за руки. Руки остались у меня. Остальное — нет.",
        )


def test_narrative_context_still_blocks_unknown_exact_phrase():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "adrian", "Адриан знает только, что Кайр может перемещаться через отражения.")
        manifest, context = _prepare(sid, "(молчать)")
        context = deepcopy(context)
        context["chronology_recent"] = [
            {"summary": "Кайр вошёл через выключенный телевизор в другой комнате."}
        ]
        _rewrite_pending_context(root, context)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid, manifest, "(молчать)",
                "**Адриан** — Ты вошёл через выключенный телевизор.",
            )
        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"] == "chronology_recent" for row in exc.value.detail["unsupported_facts"])


def test_prepare_packet_contains_compact_boundaries_without_copying_secret_facts():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        _, context = _prepare(sid, "(остаться рядом)")

        boundaries = context["knowledge_boundaries"]
        assert boundaries["mandatory"] is True
        assert "previous scene_output" in boundaries["previous_scene_rule"].casefold()
        assert "character_memory[OTHER_CHARACTER_ID]" in boundaries["global_must_not_know"]

        adrian = boundaries["characters"]["adrian"]
        assert any("character_memory[adrian]" in value for value in adrian["may_know"])
        assert adrian["must_not_know"] == "global_must_not_know"

        rendered = json.dumps(boundaries, ensure_ascii=False).casefold()
        assert "выключенный телевизор" not in rendered
        assert "10 минут" not in rendered


def test_general_known_mechanism_does_not_authorize_specific_television_detail():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        manifest, _ = _prepare(sid, "(стоять рядом)")

        _validate(
            sid,
            manifest,
            "(стоять рядом)",
            "**Адриан** — Ты можешь перемещаться через отражения.",
        )

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(стоять рядом)",
                "**Адриан** — Ты вошёл сюда через выключенный телевизор.",
            )

        detail = exc.value.detail
        assert detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert detail["character_id"] == "adrian"
        assert "телевизор" in detail["unsupported_text"].casefold()
        assert any("телевизор" in row["fact"].casefold() for row in detail["unsupported_facts"])


def test_exact_unknown_number_is_rejected_as_specificity_leak():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр может стереть только последние 10 минут памяти.")
        _seed(root, "adrian", "Адриан знает, что Кайр умеет стирать память.")

        manifest, _ = _prepare(sid, "(молчать)")

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Значит, ты можешь стереть только последние 10 минут?",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert exc.value.detail["leaked_numbers"] == ["10"]


def test_fact_explicitly_said_this_turn_is_allowed_for_physical_listener():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        raw = "Я вошёл сюда через выключенный телевизор."
        manifest, _ = _prepare(sid, raw)

        _validate(
            sid,
            manifest,
            raw,
            "**Адриан** — То есть ты вошёл через выключенный телевизор.",
        )


def test_hidden_from_self_branch_is_neither_authorized_nor_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        adrian = next(row for row in novel["characters"] if row["character_id"] == "adrian")
        adrian["sealed_truth"] = {
            "hidden_from_self": True,
            "text": "Адриан был клинически мёртв ровно 9 минут.",
        }
        sid = storage.create_session(novel)["session_id"]

        manifest, context = _prepare(sid, "(молчать)")

        own_card = next(row for row in context["character_cards"] if row["character_id"] == "adrian")
        assert "9 минут" in json.dumps(own_card, ensure_ascii=False)
        assert "9 минут" not in scene_knowledge_guard._self_card_text(own_card)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Я был клинически мёртв ровно 9 минут.",
            )

        detail = exc.value.detail
        assert detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"].startswith("self_hidden_card:") for row in detail["unsupported_facts"])


def test_other_character_card_is_not_npc_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        mira = next(row for row in novel["characters"] if row["character_id"] == "mira")
        mira["background"] = "Мира спрятала латунный ключ внутри красной вазы."
        sid = storage.create_session(novel)["session_id"]

        manifest, _ = _prepare(sid, "(молчать)")

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Ты спрятала латунный ключ внутри красной вазы.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"] == "character_card:mira" for row in exc.value.detail["unsupported_facts"])


def test_canon_note_in_packet_is_director_context_not_personal_knowledge():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["canon_notes"] = [
            {
                "note_id": "door_rule",
                "subjects": ["adrian"],
                "text": "Старая дверь открывается только латунным ключом из красной шкатулки.",
            }
        ]
        sid = storage.create_session(novel)["session_id"]

        manifest, context = _prepare(sid, "(молчать)")
        assert "canon_notes_context" in context

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Старая дверь открывается только латунным ключом из красной шкатулки.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"] == "canon_notes_context" for row in exc.value.detail["unsupported_facts"])


def test_previous_scene_output_in_packet_is_not_epistemic_authority():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        manifest, context = _prepare(sid, "(молчать)")
        context = deepcopy(context)
        context["recent_turns"] = [
            {
                "turn_number": 99,
                "user_input": "(молчать)",
                "scene_output": "**Адриан** — Ты вошёл через выключенный телевизор.",
            }
        ]
        _rewrite_pending_context(root, context)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Ты ведь вошёл через выключенный телевизор.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"] == "recent_turns" for row in exc.value.detail["unsupported_facts"])


def test_whisper_does_not_become_available_to_everyone_in_room():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        secret = "Кодовое слово синяя комета."
        _seed(root, "mira", secret)
        _seed(root, "kair", secret)

        manifest, _ = _prepare(sid, "(молчать)")
        scene = (
            "**Мира** — *(шёпотом Кайру)* Кодовое слово синяя комета.\n\n"
            "**Адриан** — Значит, кодовое слово синяя комета."
        )

        with pytest.raises(HTTPException) as exc:
            _validate(sid, manifest, "(молчать)", scene)

        assert exc.value.detail["character_id"] == "adrian"
        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"


def test_public_same_turn_speech_can_be_used_by_another_present_npc():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        secret = "Кодовое слово синяя комета."
        _seed(root, "mira", secret)

        manifest, _ = _prepare(sid, "(молчать)")
        scene = (
            "**Мира** — Кодовое слово синяя комета.\n\n"
            "**Адриан** — Понял. Кодовое слово синяя комета."
        )

        _validate(sid, manifest, "(молчать)", scene)


def test_text_message_remote_npc_does_not_hear_physical_pov_speech():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "mira"]
        novel["starting_state"]["current"]["remote_characters"] = ["adrian"]
        novel["starting_state"]["current"]["remote_channels"] = {"adrian": "сообщения"}
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кодовое слово синяя комета.")

        raw = "Кодовое слово синяя комета."
        manifest, _ = _prepare(sid, raw)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                raw,
                "**Адриан** (сообщение) — Кодовое слово синяя комета?",
            )

        assert exc.value.detail["character_id"] == "adrian"


def test_voice_call_remote_npc_can_hear_current_pov_speech():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "mira"]
        novel["starting_state"]["current"]["remote_characters"] = ["adrian"]
        novel["starting_state"]["current"]["remote_channels"] = {"adrian": "звонок"}
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кодовое слово синяя комета.")

        raw = "Кодовое слово синяя комета."
        manifest, _ = _prepare(sid, raw)

        _validate(
            sid,
            manifest,
            raw,
            "**Адриан** (звонок) — Понял. Кодовое слово синяя комета.",
        )


def test_knowledge_dependent_standalone_npc_action_is_checked():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Запасной ключ спрятан внутри красной вазы.")

        manifest, _ = _prepare(sid, "(молчать)")

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "Адриан проверил красную вазу и достал спрятанный внутри запасной ключ.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert exc.value.detail["unit_kind"] == "action"
        assert exc.value.detail["character_id"] == "adrian"






def test_observable_pov_stage_action_is_available_to_present_npc():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Телевизор был включён, пока Кайр сам его не выключил.")

        raw = "(выключить телевизор)"
        manifest, _ = _prepare(sid, raw)

        _validate(
            sid,
            manifest,
            raw,
            "**Адриан** — Ты только что выключил телевизор.",
        )


def test_private_pov_thought_inside_stage_direction_is_not_available_to_npc():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кодовое слово синяя комета.")

        raw = "(подумать, что кодовое слово синяя комета)"
        manifest, _ = _prepare(sid, raw)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                raw,
                "**Адриан** — Кодовое слово синяя комета.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert exc.value.detail["character_id"] == "adrian"


def test_scene_builder_remote_label_format_is_parsed_by_private_knowledge_runtime():
    cards = _novel()["characters"]
    units = private_knowledge_runtime._speaker_units(
        "**Адриан** (сообщение) — Я получил сообщение.",
        cards,
    )

    assert len(units) == 1
    assert units[0]["character_id"] == "adrian"
    assert "(сообщение)" in units[0]["speaker_label"]


def test_broad_director_context_needs_stronger_overlap_to_avoid_normal_dialogue_false_positive():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest, context = _prepare(sid, "(молчать)")
        context = deepcopy(context)
        context["future_guidance"] = {
            "story_direction": "Адриан должен обсудить красную дверь после семейного разговора."
        }
        _rewrite_pending_context(root, context)

        _validate(
            sid,
            manifest,
            "(молчать)",
            "**Адриан** — Красная дверь закрыта.",
        )


def test_broad_director_context_still_rejects_strong_specific_copy():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest, context = _prepare(sid, "(молчать)")
        context = deepcopy(context)
        context["future_guidance"] = {
            "story_direction": "Адриан должен открыть красную дверь латунным ключом."
        }
        _rewrite_pending_context(root, context)

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Я открою красную дверь латунным ключом.",
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(row["source"] == "future_guidance" for row in exc.value.detail["unsupported_facts"])




def test_offscreen_bundle_read_is_tracked_without_rewriting_writer_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "adrian"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        manifest, _ = _prepare(sid, "(молчать)")
        before = storage._read_json(root / "turn_packet.json", {})
        chunks_before = list(before["chunks"])

        read = character_chunk_read.prepare_character_bundle_read(sid, "mira")
        tracked = storage._read_json(root / "turn_packet.json", {})

        assert tracked["packet_id"] == manifest["packet_id"]
        assert tracked["chunks"] == chunks_before
        assert tracked["character_bundle_reads"]["mira"]["read_id"] == read["read_id"]
        assert tracked["character_bundle_reads"]["mira"]["read_chunks"] == [0]




def test_reused_pending_prepare_keeps_bundle_read_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "adrian"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid

        first, _ = _prepare(sid, "(молчать)")
        read = character_chunk_read.prepare_character_bundle_read(sid, "mira")

        second = session_runtime.prepare_turn_packet(sid, "(молчать)")
        packet = storage._read_json(root / "turn_packet.json", {})

        assert second["packet_id"] == first["packet_id"]
        assert second["reused_pending_packet"] is True
        assert packet["character_bundle_reads"]["mira"]["read_id"] == read["read_id"]
        assert packet["character_bundle_reads"]["mira"]["read_chunks"] == [0]


def test_foreign_fact_seen_only_through_offscreen_bundle_cannot_leak_to_other_npc():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "adrian"]
        sid = storage.create_session(novel)["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "mira", "Мира хранит латунный ключ внутри синего чемодана.")

        manifest, context = _prepare(sid, "(молчать)")
        assert "mira" not in context["character_memory"]

        character_chunk_read.prepare_character_bundle_read(sid, "mira")

        with pytest.raises(HTTPException) as exc:
            _validate(
                sid,
                manifest,
                "(молчать)",
                "**Адриан** — Мира хранит латунный ключ внутри синего чемодана.",
            )

        detail = exc.value.detail
        assert detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(
            row["source"].startswith("loaded_bundle_chunk:mira:")
            for row in detail["unsupported_facts"]
        )




def test_unread_offscreen_bundle_chunks_do_not_become_protected_visibility():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        novel = _novel()
        novel["starting_state"]["current"]["present_characters"] = ["kair", "adrian"]
        mira = next(row for row in novel["characters"] if row["character_id"] == "mira")
        mira["background"] = "A" * 13000 + " Фраза из непрочитанного хвоста: янтарный компас под лестницей."
        sid = storage.create_session(novel)["session_id"]

        manifest, _ = _prepare(sid, "(молчать)")
        read = character_chunk_read.prepare_character_bundle_read(sid, "mira")
        assert read["chunk_count"] > 1

        # Only chunk 0 was returned. The hidden phrase is deliberately in a later
        # unread chunk, so it must not be treated as something the model saw.
        _validate(
            sid,
            manifest,
            "(молчать)",
            "**Адриан** — Янтарный компас лежит под лестницей.",
        )


def test_rejected_commit_keeps_pending_turn_uncommitted():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        manifest, _ = _prepare(sid, "(молчать)")

        with pytest.raises(HTTPException) as exc:
            session_runtime.commit_turn(
                sid,
                _payload(
                    manifest["packet_id"],
                    "(молчать)",
                    "**Адриан** — Ты вошёл сюда через выключенный телевизор.",
                ),
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        pending = storage._read_json(root / "turn_packet.json", {})
        assert pending["packet_id"] == manifest["packet_id"]
        assert storage._read_turns(root) == []


def test_pipeline_version_bump_invalidates_old_pending_packet():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid

        first, _ = _prepare(sid, "(молчать)")
        packet = storage._read_json(root / "turn_packet.json", {})
        packet["turn_pipeline_version"] = 18
        storage._write_json(root / "turn_packet.json", packet)

        second = session_runtime.prepare_turn_packet(sid, "(молчать)")

        assert second["packet_id"] != first["packet_id"]
        assert second["turn_pipeline_version"] == 19
