import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.novel_drafts import create_draft, create_session_from_draft, draft_status, finalize_draft, save_section


def _setup(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _base(draft_id: str):
    save_section(draft_id, "novel", json.dumps({"pov_character": "rina", "genres": ["триллер"]}, ensure_ascii=False))
    save_section(draft_id, "characters", json.dumps([
        {"character_id": "rina", "name": "Рината", "is_pov": True},
        {"character_id": "adrian", "name": "Эдриан"},
    ], ensure_ascii=False))
    save_section(draft_id, "lore", json.dumps({"war": "идёт война"}, ensure_ascii=False))
    save_section(draft_id, "starting_state", json.dumps({
        "pov": {"character_id": "rina"},
        "current": {"date": "02.09.2026", "time": "08:00", "location": "база", "present_characters": ["rina"]},
    }, ensure_ascii=False))


def test_existing_saved_draft_normalizes_scalar_storage_usage_and_hook_aliases():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = create_draft("coverage-aliases", "Coverage Aliases", version=2)["draft_id"]
        _base(draft_id)
        foundation = {
            "facts": [
                {
                    "fact_id": "f_war",
                    "text": "В мире идёт война.",
                    "source": "player",
                    "stored_at": "lore.war",
                    "usage": "сюжетный крючок",
                }
            ],
            "hooks": {
                "war_hook": {
                    "fact_id": "f_war",
                    "condition": "когда война естественно входит в сцену",
                    "status": "latent",
                }
            },
            "story_pillars": ["война и экшн"],
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert status["foundation_coverage"]["unmapped"] == []

        final = finalize_draft(draft_id)
        assert final["foundation_coverage"]["fact_count"] == 1
        created = create_session_from_draft(draft_id)
        state = storage._read_json(storage.SESSIONS_DIR / created["session_id"] / "state.json", {})
        assert state["world"]["foundation_hook_state"]["war_hook"]["status"] == "latent"


def test_missing_storage_and_unknown_usage_do_not_lose_fact_or_block_finalize():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = create_draft("coverage-self-map", "Coverage Self Map", version=2)["draft_id"]
        _base(draft_id)
        foundation = {
            "facts": [
                {
                    "fact_id": "f_detail",
                    "text": "У POV есть важная деталь прошлого.",
                    "source": "player",
                    "story_use": "учитывать в истории",
                }
            ],
            "hooks": [],
            "story_pillars": ["личная линия"],
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        assert draft_status(draft_id)["ready_to_finalize"] is True
        finalize_draft(draft_id)
        draft = json.loads((storage.DATA_DIR / "novel_drafts" / f"{draft_id}.json").read_text(encoding="utf-8"))
        fact = draft["finalized_template"]["foundation"]["facts"][0]
        assert fact["stored_in"] == ["foundation.facts.f_detail"]
        assert fact["story_use"] == "reference"


def test_hook_usage_without_explicit_hook_gets_safe_latent_hook():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = create_draft("coverage-auto-hook", "Coverage Auto Hook", version=2)["draft_id"]
        _base(draft_id)
        foundation = {
            "facts": [
                {
                    "fact_id": "f_brother",
                    "text": "У POV пропал брат.",
                    "source": "player",
                    "section": "characters.rina.background",
                    "story_use": "hook",
                }
            ],
            "hooks": [],
            "story_pillars": ["личная тайна"],
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        assert draft_status(draft_id)["ready_to_finalize"] is True
        finalize_draft(draft_id)
        draft = json.loads((storage.DATA_DIR / "novel_drafts" / f"{draft_id}.json").read_text(encoding="utf-8"))
        hooks = draft["finalized_template"]["foundation"]["hooks"]
        assert len(hooks) == 1
        assert hooks[0]["fact_ids"] == ["f_brother"]
        assert hooks[0]["status"] == "latent"


def test_unknown_fact_reference_still_blocks_after_normalization():
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = create_draft("coverage-bad-ref", "Coverage Bad Ref", version=2)["draft_id"]
        _base(draft_id)
        foundation = {
            "facts": [{"fact_id": "f_real", "text": "Реальный факт.", "story_use": "reference"}],
            "hooks": [{"hook_id": "bad", "fact_ids": "does_not_exist"}],
            "story_pillars": ["линия"],
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] == "FOUNDATION_HOOK_UNKNOWN_FACT"
        with pytest.raises(ValueError, match="FOUNDATION_HOOK_UNKNOWN_FACT"):
            finalize_draft(draft_id)
