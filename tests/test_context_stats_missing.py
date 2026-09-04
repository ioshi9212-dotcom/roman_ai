import tempfile
from pathlib import Path

import pytest

from app import storage
from app.context_stats import session_context_stats


def test_context_stats_missing_session_raises_without_creating_files():
    with tempfile.TemporaryDirectory() as tmp:
        storage.DATA_DIR = Path(tmp)
        storage.LIBRARY_DIR = storage.DATA_DIR / "library"
        storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
        storage.ensure_dirs()
        before = list(storage.SESSIONS_DIR.iterdir())
        with pytest.raises(FileNotFoundError):
            session_context_stats("missing")
        assert list(storage.SESSIONS_DIR.iterdir()) == before
