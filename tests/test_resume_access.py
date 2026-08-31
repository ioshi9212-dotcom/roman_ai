import json
import tempfile
from pathlib import Path

from app import novel_access, resume_access, storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def _make_large_handoff_session() -> tuple[str, Path]:
    novel = {
        "novel_id": "large_resume",
        "title": "Large Resume",
        "version": 1,
        "novel": {"pov_character": "rina"},
        "characters": [{"character_id": "rina", "name": "Rina", "is_pov": True}],
        "lore": {"large": "x" * 35000},
        "starting_state": {"current": {"location": "room", "present_characters": ["rina"]}},
    }
    meta = storage.create_session(novel)
    session_id = meta["session_id"]
    root = storage.SESSIONS_DIR / session_id

    session_meta = storage._read_json(root / "meta.json", {})
    session_meta.update({
        "turn_number": 60,
        "last_audit_turn": 60,
        "audit_required": False,
        "handoff_required": True,
    })
    storage._write_json(root / "meta.json", session_meta)
    storage._write_json(
        root / "handoff_tail.json",
        [{"turn_number": turn, "scene_output": f"scene {turn}"} for turn in range(55, 61)],
    )
    return session_id, root


def test_large_resume_package_is_chunked_and_reconstructable():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id, _ = _make_large_handoff_session()

        manifest = resume_access.prepare_resume_read(session_id)
        assert manifest["session_id"] == session_id
        assert manifest["resume_token"]
        assert manifest["chunk_count"] > 1
        assert manifest["total_chars"] > resume_access.RESUME_CHUNK_CHARS

        parts = []
        for index in range(manifest["chunk_count"]):
            chunk = resume_access.get_resume_chunk(session_id, manifest["read_id"], index)
            assert chunk["chunk_index"] == index
            assert chunk["chunk_count"] == manifest["chunk_count"]
            parts.append(chunk["content"])

        assert chunk["all_chunks_read"] is True
        package = json.loads("".join(parts))
        assert package["session_id"] == session_id
        assert package["resume_token"] == manifest["resume_token"]
        assert package["meta"]["turn_number"] == 60
        assert [item["turn_number"] for item in package["handoff_tail"]] == [55, 56, 57, 58, 59, 60]
        assert package["source"]["lore"]["large"] == "x" * 35000

        confirmed = storage.confirm_resume(session_id, manifest["resume_token"])
        assert confirmed["turn_number"] == 60
        assert confirmed["handoff_generation"] == 1


def test_resume_read_id_is_compatible_with_get_novel_read_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)
        session_id, _ = _make_large_handoff_session()

        manifest = resume_access.prepare_resume_read(session_id)
        parts = []
        for index in range(manifest["chunk_count"]):
            chunk = novel_access.get_novel_read_chunk(manifest["read_id"], index)
            assert chunk["source_type"] == "resume"
            assert chunk["source_id"] == session_id
            parts.append(chunk["content"])

        package = json.loads("".join(parts))
        assert package["session_id"] == session_id
        assert package["resume_token"] == manifest["resume_token"]
