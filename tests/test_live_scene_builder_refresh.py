"""Regression checks for author-controlled scene rules and opening cast."""

from unittest.mock import patch

import pytest

from app import runtime_access
from app.turn_pipeline import _scene_ids


def test_live_builder_is_fresh_for_each_new_request(monkeypatch):
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "github")
    with patch.object(
        runtime_access, "_fetch_github_scene_builder",
        side_effect=["Первый Skin Builder", "Новый Skin Builder"],
    ) as fetch:
        with runtime_access.runtime_read_view():
            first = runtime_access.runtime_documents()
            assert first["scene_builder"] == "Первый Skin Builder"
            assert runtime_access.runtime_documents() == first
            first_revision = runtime_access.runtime_revision()
        with runtime_access.runtime_read_view():
            second = runtime_access.runtime_documents()
            assert second["scene_builder"] == "Новый Skin Builder"
            assert runtime_access.runtime_revision() != first_revision
        assert fetch.call_count == 2


def test_live_builder_outage_never_silently_uses_old_copy(monkeypatch):
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "github")
    with patch.object(
        runtime_access, "_fetch_github_scene_builder",
        side_effect=RuntimeError("SCENE_BUILDER_GITHUB_UNAVAILABLE"),
    ):
        with pytest.raises(RuntimeError, match="SCENE_BUILDER_GITHUB_UNAVAILABLE"):
            runtime_access.runtime_documents()


def test_explicit_local_mode_only_reads_checked_in_files(monkeypatch):
    monkeypatch.setenv("ROMAN_SCENE_BUILDER_SOURCE", "file")
    assert runtime_access.runtime_documents() == runtime_access.author_runtime_sources()


def test_opening_scene_excludes_offscreen_and_remote_full_cards():
    cards = [{"character_id": cid, "name": cid} for cid in ("pov", "here", "remote", "away")]
    state = {
        "pov": {"character_id": "pov"},
        "current": {
            "present_characters": ["pov", "here"],
            "remote_characters": ["remote"],
        },
    }
    assert _scene_ids(state, cards, opening_scene=True) == ["pov", "here"]
    assert _scene_ids(state, cards) == ["pov", "here", "remote"]
