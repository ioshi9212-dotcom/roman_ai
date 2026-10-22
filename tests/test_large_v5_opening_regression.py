"""Regression for large V5 drafts that create a session but fail on opening prepare.

Uses synthetic characters and text. Never copies or exposes a user's private novel.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app import draft_intake_runtime, novel_drafts, setup_draft_v3_runtime, storage
from app.main import turn_packet_prepare
from app.models import TurnPrepare
from app.novel_access import get_novel_read_chunk


def _isolate_storage(tmp: str) -> None:
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_first_scene_of_61k_raw_v5_draft_with_18_characters_is_preparable():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_storage(tmp)
        raw = ("Раздел мира и персонажей. Сведения не являются знаниями POV.\n" * 1200)[:61616]
        if len(raw) < 61616:
            raw += "Ф" * (61616 - len(raw))
        assert len(raw) == 61616

        draft_id = novel_drafts.create_draft(
            "synthetic_large_opening", "Синтетическая новелла", version=5
        )["draft_id"]
        uploaded = draft_intake_runtime.append_intake_chunk(
            draft_id, block_id="source_txt", stage="source",
            chunk_index=0, raw_text=raw, is_last=True,
        )
        assert uploaded["complete"] is True
        assert uploaded["char_count"] == len(raw)

        characters = [
            {"id": "pov_elen", "name": "Елена", "role": "POV",
             "is_pov": True, "status": "человек", "character": "настойчивая"},
            {"id": "npc_marcus", "name": "Маркус", "role": "NPC",
             "status": "человек", "character": "самостоятельный"},
        ]
        characters.extend(
            {"id": f"npc_{i:02d}", "name": f"Персонаж {i:02d}",
             "role": "NPC", "status": "человек"}
            for i in range(16)
        )
        assert len(characters) == 18

        sections = {
            "novel": {
                "genres": ["мистика", "триллер"],
                "pov_character": "pov_elen",
                "premise": "Синтетическая история с 18 персонажами.",
                "start": {"date": "1017-07-13", "location": "кофейня"},
            },
            "characters": characters,
            "hidden_lore": {"entries": ["Этот секрет никому из NPC не известен."]},
        }
        for section, value in sections.items():
            current = novel_drafts.draft_status(draft_id)["revision"]
            novel_drafts.save_section(
                draft_id, section,
                json.dumps(value, ensure_ascii=False),
                expected_revision=current,
            )
        current = novel_drafts.draft_status(draft_id)["revision"]
        draft_intake_runtime.update_intake_mapping(
            draft_id, "source_txt", fact_ids=[],
            reviewed_against_raw=True,
            contains_no_facts=False,
            expected_revision=current,
        )

        manifest = novel_drafts.prepare_draft_read(draft_id)
        for i in range(manifest["chunk_count"]):
            get_novel_read_chunk(manifest["read_id"], i)
        current = novel_drafts.draft_status(draft_id)["revision"]
        setup_draft_v3_runtime.confirm_reconciliation(
            draft_id, expected_revision=current,
            confirmed_against_raw=True, unresolved_conflicts=[],
        )
        final = novel_drafts.finalize_draft(draft_id)
        assert final["content_finalized"] is True
        finalized_revision = novel_drafts._read(draft_id)["finalized_revision"]

        ready = setup_draft_v3_runtime.set_launch_state(
            draft_id, expected_finalized_revision=finalized_revision,
            starting_state_json=json.dumps({
                "game_day": 1,
                "date": "1017-07-13",
                "time": "день, точное время не установлено",
                "location": "кофейня",
                "scene": "Начало в кофейне.",
                "present_characters": ["pov_elen", "npc_marcus"],
                "weather": "не установлена",
                "inventory": ["гитара"],
            }, ensure_ascii=False),
        )
        assert ready["session_ready"] is True
        created = novel_drafts.create_session_from_draft(draft_id)
        session_id = created["session_id"]
        assert created["turn_number"] == 0

        first = turn_packet_prepare(session_id, TurnPrepare(
            user_input="", opening_scene=True,
            request_id="opening-synthetic-001",
        ))
        assert first["packet_id"]
        assert first["opening_scene"] is True
        assert first["prepared_for_turn"] == 1

        same = turn_packet_prepare(session_id, TurnPrepare(
            user_input="", opening_scene=True,
            request_id="opening-synthetic-001",
        ))
        assert same["packet_id"] == first["packet_id"]
        assert storage._read_json(
            storage.SESSIONS_DIR / session_id / "meta.json", {}
        )["turn_number"] == 0
