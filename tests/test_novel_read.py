import json
import tempfile
from pathlib import Path

from app import draft_intake_runtime, storage
from app.novel_access import get_novel_read_chunk, prepare_novel_read, verify_novel
from app.novel_drafts import create_draft, finalize_draft, prepare_draft_read, save_section


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_large_library_novel_is_verified_without_full_response_and_can_be_read_in_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        huge_lore = {"archive": "x" * 40000}
        novel = {
            "novel_id": "large_novel",
            "title": "Large Novel",
            "version": 1,
            "novel": {"genre": "romance", "pov_character": "rina"},
            "characters": [
                {"character_id": "rina", "name": "Rina", "is_pov": True},
                {"character_id": "liam", "name": "Liam"},
            ],
            "lore": huge_lore,
            "rules": {"tone": "cinematic"},
            "hidden_lore": {"secret": "hidden"},
            "starting_state": {"current": {"location": "room", "present_characters": ["rina"]}},
        }
        storage.save_novel(novel)

        verification = verify_novel("large_novel")
        assert verification["ok"] is True
        assert verification["character_count"] == 2
        assert verification["sections"]["lore"]["chars"] > 40000
        assert "archive" not in json.dumps(verification)

        manifest = prepare_novel_read("large_novel")
        assert manifest["chunk_count"] >= 4
        text = ""
        for index in range(manifest["chunk_count"]):
            chunk = get_novel_read_chunk(manifest["read_id"], index)
            text += chunk["content"]
        assert chunk["all_chunks_read"] is True
        reconstructed = json.loads(text)
        assert reconstructed["lore"]["archive"] == huge_lore["archive"]


def test_finalize_keeps_large_draft_out_of_library_and_allows_chunked_verification():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft = create_draft("draft_large", "Draft Large")
        draft_id = draft["draft_id"]
        save_section(draft_id, "novel", json.dumps({"pov_character": "rina"}))
        save_section(draft_id, "characters", json.dumps([{"character_id": "rina", "name": "Rina", "is_pov": True}]))
        save_section(draft_id, "lore", json.dumps({"big": "y" * 30000}))
        save_section(
            draft_id,
            "starting_state",
            json.dumps({"current": {"location": "room", "present_characters": ["rina"]}}),
        )
        result = finalize_draft(draft_id)
        assert result["ok"] is True
        assert result["verification"]["ok"] is True
        assert result["verification"]["character_count"] == 1
        assert result["saved_to_library"] is False
        assert storage.list_novels() == []
        assert "big" not in json.dumps(result)

        manifest = prepare_draft_read(draft_id)
        text = ""
        for index in range(manifest["chunk_count"]):
            chunk = get_novel_read_chunk(manifest["read_id"], index)
            text += chunk["content"]
        reconstructed = json.loads(text)
        assert reconstructed["lore"]["big"] == "y" * 30000

def test_working_draft_prepare_resumes_same_revision_from_first_unread_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("resume_read", "Resume read", version=5)["draft_id"]
        raw = ("Большой RAW блок с деталями персонажа и мира. " * 500)
        pieces = [raw[i:i + 5500] for i in range(0, len(raw), 5500)]
        for index, piece in enumerate(pieces):
            draft_intake_runtime.append_intake_chunk(
                draft_id,
                block_id="raw_setup",
                stage="setup",
                chunk_index=index,
                raw_text=piece,
                is_last=index == len(pieces) - 1,
            )

        first = prepare_draft_read(draft_id)
        assert first["chunk_count"] > 2
        assert first["next_chunk_index"] == 0
        get_novel_read_chunk(first["read_id"], 0)
        get_novel_read_chunk(first["read_id"], 1)

        resumed = prepare_draft_read(draft_id)
        assert resumed["read_id"] == first["read_id"]
        assert resumed["resumed_read"] is True
        assert resumed["read_chunk_count"] == 2
        assert resumed["next_chunk_index"] == 2


def test_v5_working_read_does_not_duplicate_raw_as_source_units():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        draft_id = create_draft("v5_raw_once", "V5 raw once", version=5)["draft_id"]
        raw = "Рина работает в архиве. Она боится воды. У неё зелёные глаза."
        draft_intake_runtime.append_intake_chunk(
            draft_id,
            block_id="raw_setup",
            stage="setup",
            chunk_index=0,
            raw_text=raw,
            is_last=True,
        )

        manifest = prepare_draft_read(draft_id)
        parts = []
        index = manifest["next_chunk_index"]
        while index is not None:
            row = get_novel_read_chunk(manifest["read_id"], index)
            parts.append(row["content"])
            index = row["next_chunk_index"]
        payload = json.loads("".join(parts))
        block = payload["sections"]["intake"]["blocks"][0]
        assert block["raw_text"] == raw
        assert "source_units" not in block
        assert "source_unit_count" not in block

