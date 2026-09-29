import json
import tempfile
from pathlib import Path

from app import character_chunk_read, runtime_access, session_runtime, simple_setup_runtime, storage
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
        ):
            assert removed not in context

        assert list(context)[-2:] == ["runtime_rules", "scene_builder"]
        active = {row["character_id"] for row in context["character_cards"]}
        assert active == {"pov", "npc"}
        assert "away" not in active
        assert context["working_context_contract"]["hidden_director_guard_layers"] is False


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

        _, context = read_context(sid, "(посмотреть на NPC)")
        rows = context["character_memory"]["npc"]["knowledge_journal"]
        assert len(rows) == 120
        assert rows[0]["text"] == "fact 1"
        assert rows[-1]["text"] == "fact 120"


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


def test_turn_commit_schema_no_longer_requires_review_flags_or_exact_scene_format():
    model = TurnCommit(
        packet_id="packet",
        user_input="test",
        scene_output="Короткая тестовая сцена.",
        extracted={},
    )
    assert model.extracted.chronology == []
    assert model.extracted.knowledge_add == []
    assert not hasattr(model.extracted, "knowledge_reviewed")


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
    template = simple_setup_runtime._content_template(draft)
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
        assert bundle["knowledge_scope"]["own_profile_is_self_known"] is True
