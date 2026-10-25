"""Large TXT intake must resume automatically and prove full-byte integrity."""
import hashlib
import json
import tempfile
from pathlib import Path

import yaml

from app import draft_intake_runtime, novel_drafts, storage
from app.main import novel_draft_intake_chunk_append
from app.models import NovelDraftIntakeChunk


def _setup(tmp):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_v5_multichunk_upload_reassembles_verbatim_55k_unicode_and_returns_sha():
    raw = ("Рина: «Не смей смягчать! 🤬»\nСайлас скрывает своё имя.  \n"
           "Данте обещал: будет дождь.\r\n") * 900
    chunks = [raw[i:i + 5000] for i in range(0, len(raw), 5000)]
    assert len(raw) > 50000 and len(chunks) > 10
    with tempfile.TemporaryDirectory() as tmp:
        _setup(tmp)
        draft_id = novel_drafts.create_draft("novel_auto_raw", "ПОКА МИР НЕ СГОРИТ", 5)["draft_id"]
        result = None
        for i, chunk in enumerate(chunks):
            result = novel_draft_intake_chunk_append(
                draft_id,
                NovelDraftIntakeChunk(block_id="original_raw", stage="setup",
                    chunk_index=i, raw_text=chunk, full_raw=False,
                    is_last=i == len(chunks) - 1),
            )
            assert result["draft_id"] == draft_id
            assert "не отвечай игроку" in result["instruction"] if i < len(chunks) - 1 else True
            if i < len(chunks) - 1:
                assert result["complete"] is False
                assert result["next_chunk_index"] == i + 1
            else:
                assert result["complete"] is True
                assert result["char_count"] == len(raw)
                assert result["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()

        read_back = novel_drafts._read(draft_id)["sections"]["intake"]["blocks"][0]["raw_text"]
        assert read_back == raw

        # When the final response was lost, retry the same final chunk:
        replay = novel_draft_intake_chunk_append(
            draft_id, NovelDraftIntakeChunk(block_id="original_raw", stage="setup",
                chunk_index=len(chunks)-1, raw_text=chunks[-1], full_raw=False, is_last=True)
        )
        assert replay["complete"] is True
        assert replay["already_completed"] is True
        assert replay["raw_sha256"] == result["raw_sha256"]
        assert replay["char_count"] == len(raw)


def test_schema_mandates_chunk_progress_and_autonomous_gpt_setup():
    root = Path(__file__).parents[1]
    schema = yaml.safe_load((root / "openapi.yaml").read_text(encoding="utf-8"))
    operations = [(p, m) for p, methods in schema["paths"].items()
                  for m, value in methods.items() if isinstance(value, dict) and "operationId" in value]
    assert len(operations) == 30
    response = schema["paths"]["/novel-drafts/{draft_id}/intake/chunks"]["post"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/NovelDraftIntakeProgress")
    progress = schema["components"]["schemas"]["NovelDraftIntakeProgress"]
    assert {"draft_id", "block_id", "complete", "char_count"} <= set(progress["required"])
    assert "raw_sha256" in progress["properties"]

    instructions = (root / "gpt" / "custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "НЕ отвечай промежуточными статусами" in instructions
    assert "НЕ жди новых команд/подтверждений" in instructions
    assert "full_raw=false" in instructions
    assert "next_chunk_index" in instructions
    assert "finalizeNovelDraft" in instructions
    assert len(instructions) + instructions.count("\n") < 8000
