import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import scene_knowledge_guard, session_runtime, storage


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


def _payload(packet_id: str, user_input: str, scene_output: str):
    return {
        "packet_id": packet_id,
        "user_input": user_input,
        "scene_output": scene_output,
        "extracted": {},
    }


def test_prepare_packet_contains_explicit_per_npc_knowledge_boundaries():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        manifest = session_runtime.prepare_turn_packet(sid, "(остаться рядом)")
        context = _read_packet(manifest, sid)

        boundaries = context["knowledge_boundaries"]
        assert boundaries["mandatory"] is True
        assert "previous scene_output" in boundaries["narrative_continuity_only_not_knowledge"]
        adrian = boundaries["characters"]["adrian"]
        assert any("отражен" in fact.casefold() for fact in adrian["may_know"])
        assert any("телевизор" in row["fact"].casefold() for row in adrian["must_not_know"])


def test_general_known_mechanism_does_not_authorize_specific_television_detail():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        scene_knowledge_guard.validate_scene_output(
            sid,
            {
                "user_input": "(стоять рядом)",
                "scene_output": "**Адриан** — Ты можешь перемещаться через отражения.",
                "extracted": {},
            },
        )

        with pytest.raises(HTTPException) as exc:
            scene_knowledge_guard.validate_scene_output(
                sid,
                {
                    "user_input": "(стоять рядом)",
                    "scene_output": "**Адриан** — Ты вошёл сюда через выключенный телевизор.",
                    "extracted": {},
                },
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

        with pytest.raises(HTTPException) as exc:
            scene_knowledge_guard.validate_scene_output(
                sid,
                {
                    "user_input": "(молчать)",
                    "scene_output": "**Адриан** — Значит, ты можешь стереть только последние 10 минут?",
                    "extracted": {},
                },
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert exc.value.detail["leaked_numbers"] == ["10"]


def test_fact_explicitly_said_this_turn_is_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        scene_knowledge_guard.validate_scene_output(
            sid,
            {
                "user_input": "Я вошёл сюда через выключенный телевизор.",
                "scene_output": "**Адриан** — То есть ты вошёл через выключенный телевизор.",
                "extracted": {},
            },
        )


def test_previous_scene_output_is_not_epistemic_authority():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        old_turn = {
            "turn_number": 1,
            "user_input": "(молчать)",
            "scene_output": "**Адриан** — Ты вошёл через выключенный телевизор.",
            "extracted": {},
        }
        (root / "turns.jsonl").write_text(
            json.dumps(old_turn, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(HTTPException) as exc:
            scene_knowledge_guard.validate_scene_output(
                sid,
                {
                    "user_input": "(молчать)",
                    "scene_output": "**Адриан** — Ты ведь вошёл через выключенный телевизор.",
                    "extracted": {},
                },
            )

        assert exc.value.detail["code"] == "SCENE_NPC_KNOWLEDGE_LEAK"
        assert any(
            row["source"].startswith("prior_scene_output:")
            for row in exc.value.detail["unsupported_facts"]
        )


def test_rejected_commit_keeps_pending_turn_uncommitted():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        sid = storage.create_session(_novel())["session_id"]
        root = storage.SESSIONS_DIR / sid
        _seed(root, "kair", "Кайр вошёл в квартиру Миры через выключенный телевизор.")
        _seed(root, "adrian", "Адриан знает, что Кайр может перемещаться через отражения.")

        manifest = session_runtime.prepare_turn_packet(sid, "(молчать)")
        _read_packet(manifest, sid)

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
