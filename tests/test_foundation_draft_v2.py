import json
import tempfile
from pathlib import Path

import pytest

from app import storage
from app.novel_drafts import create_draft, create_session_from_draft, draft_status, finalize_draft, save_section


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def save_base(draft_id: str):
    save_section(draft_id, "novel", json.dumps({"pov_character": "rina", "genres": ["триллер", "экшн"]}, ensure_ascii=False))
    save_section(draft_id, "characters", json.dumps([
        {"character_id": "rina", "name": "Рината", "is_pov": True, "background": {"missing_brother": True}},
        {"character_id": "adrian", "name": "Эдриан"},
    ], ensure_ascii=False))
    save_section(draft_id, "lore", json.dumps({"war": "идёт война"}, ensure_ascii=False))
    save_section(draft_id, "starting_state", json.dumps({
        "pov": {"character_id": "rina"},
        "current": {"date": "17.07.1017", "time": "08:00", "location": "база", "present_characters": ["rina"]},
    }, ensure_ascii=False))


def complete_foundation():
    return {
        "facts": [
            {
                "fact_id": "f_missing_brother",
                "text": "У POV пропавший без вести брат.",
                "source": "player",
                "stored_in": ["characters[rina].background.missing_brother"],
                "story_use": "hook",
            },
            {
                "fact_id": "f_war",
                "text": "В мире идёт война.",
                "source": "player",
                "stored_in": ["lore.war", "novel.genres"],
                "story_use": "hook",
            },
        ],
        "hooks": [
            {"hook_id": "h_brother", "fact_ids": ["f_missing_brother"], "condition": "когда прошлое, семья или поиск естественно становятся уместны", "status": "latent", "pillar_ids": ["personal_mystery"]},
            {"hook_id": "h_war", "fact_ids": ["f_war"], "condition": "когда внешняя обстановка или задание причинно выводят войну в сцену", "status": "latent", "pillar_ids": ["war_action"]},
        ],
        "story_pillars": [
            {"pillar_id": "personal_mystery", "label": "личная тайна прошлого", "source_fact_ids": ["f_missing_brother"]},
            {"pillar_id": "war_action", "label": "война и экшн", "source_fact_ids": ["f_war"]},
        ],
    }


def test_v2_draft_cannot_finalize_without_foundation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-missing", "V2 Missing", version=2)["draft_id"]
        save_base(draft_id)
        status = draft_status(draft_id)
        assert "foundation" in status["missing_required_sections"]
        assert status["ready_to_finalize"] is False


def test_v2_foundation_rejects_unmapped_or_unhooked_player_fact():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-bad", "V2 Bad", version=2)["draft_id"]
        save_base(draft_id)
        bad = complete_foundation()
        bad["hooks"] = [row for row in bad["hooks"] if row["hook_id"] != "h_brother"]
        save_section(draft_id, "foundation", json.dumps(bad, ensure_ascii=False))
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"].startswith("FOUNDATION_COVERAGE_INCOMPLETE")
        with pytest.raises(ValueError):
            finalize_draft(draft_id)


def test_complete_v2_foundation_finalizes_and_seeds_live_hook_and_pillar_state():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-good", "V2 Good", version=2)["draft_id"]
        save_base(draft_id)
        save_section(draft_id, "foundation", json.dumps(complete_foundation(), ensure_ascii=False))
        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert status["foundation_coverage"]["unmapped"] == []
        final = finalize_draft(draft_id)
        assert final["foundation_coverage"]["fact_count"] == 2
        created = create_session_from_draft(draft_id)
        root = storage.SESSIONS_DIR / created["session_id"]
        state = storage._read_json(root / "state.json", {})
        assert state["world"]["foundation_hook_state"]["h_brother"]["status"] == "latent"
        assert state["world"]["story_pillars"]["war_action"]["label"] == "война и экшн"
        assert state["world"]["social"]["signals"] == {}


def test_story_pillars_accept_simple_strings_and_finalize_without_resaving_foundation():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-simple-pillars", "Simple Pillars", version=2)["draft_id"]
        save_base(draft_id)
        foundation = complete_foundation()
        foundation["hooks"][0]["pillar_ids"] = ["личная тайна прошлого"]
        foundation["hooks"][1]["pillar_ids"] = ["война и экшн"]
        foundation["story_pillars"] = ["личная тайна прошлого", "война и экшн"]
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        assert status["foundation_coverage"]["pillar_count"] == 2

        finalize_draft(draft_id)
        created = create_session_from_draft(draft_id)
        root = storage.SESSIONS_DIR / created["session_id"]
        state = storage._read_json(root / "state.json", {})
        labels = {row["label"] for row in state["world"]["story_pillars"].values()}
        assert labels == {"личная тайна прошлого", "война и экшн"}


def test_story_pillars_accept_mapping_aliases_and_scalar_fact_reference():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-map-pillars", "Mapped Pillars", version=2)["draft_id"]
        save_base(draft_id)
        foundation = complete_foundation()
        foundation["story_pillars"] = {
            "personal_mystery": {"title": "личная тайна прошлого", "fact_ids": "f_missing_brother"},
            "war_action": {"name": "война и экшн", "facts": ["f_war"]},
        }
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is True
        final = finalize_draft(draft_id)
        assert final["foundation_coverage"]["pillar_count"] == 2
        created = create_session_from_draft(draft_id)
        root = storage.SESSIONS_DIR / created["session_id"]
        state = storage._read_json(root / "state.json", {})
        assert state["world"]["story_pillars"]["personal_mystery"]["source_fact_ids"] == ["f_missing_brother"]
        assert state["world"]["story_pillars"]["war_action"]["source_fact_ids"] == ["f_war"]


def test_story_pillar_unknown_fact_still_blocks_finalize_after_normalization():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v2-bad-pillar-fact", "Bad Pillar Fact", version=2)["draft_id"]
        save_base(draft_id)
        foundation = complete_foundation()
        foundation["story_pillars"] = [{"title": "война", "facts": "does_not_exist"}]
        save_section(draft_id, "foundation", json.dumps(foundation, ensure_ascii=False))

        status = draft_status(draft_id)
        assert status["ready_to_finalize"] is False
        assert status["finalize_blocker"] == "FOUNDATION_STORY_PILLAR_UNKNOWN_FACT"
