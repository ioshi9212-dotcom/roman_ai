"""Architecture regression: one canonical gameplay pipeline, no old wrapper chains.

The compatibility aliases in session_runtime exist for historical call sites, but
production Actions must address turn_pipeline explicitly.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app import main, operation_service, session_runtime, turn_pipeline


def test_live_action_handlers_bind_to_canonical_pipeline():
    # Routes and service logic must not depend on import-order monkeypatches
    # to select the currently active implementation of a gameplay request.
    assert main.continue_session is turn_pipeline.continue_session
    assert operation_service.turn_pipeline is turn_pipeline

    code = inspect.getsource(operation_service)
    assert "session_runtime.prepare_turn_packet(" not in code
    assert "session_runtime.commit_turn(" not in code
    assert "session_runtime.commit_audit(" not in code

    # Backward-compatible access is still available for historic callers.
    assert session_runtime.prepare_turn_packet is turn_pipeline.prepare_turn_packet
    assert session_runtime.commit_turn is turn_pipeline.commit_turn
    assert session_runtime.commit_audit is turn_pipeline.commit_audit
    assert session_runtime.continue_session is turn_pipeline.continue_session


def test_fresh_session_reader_cannot_reference_undefined_override_locals():
    # These three unbound names caused or threatened sequential 500s on Amvera.
    source = inspect.getsource(session_runtime._refresh_session_familiarity)
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names.intersection({"source_override", "state_override", "return_snapshot"})
    assert "root / \"source.json\"" in source
    assert "root / \"state.json\"" in source


def test_superseded_relationship_installers_are_no_longer_shipped():
    root = Path(__file__).resolve().parents[1] / "app"
    assert not (root / "relationship_growth_runtime.py").exists()
    assert not (root / "relationship_policy_runtime.py").exists()

    boot = (root / "__init__.py").read_text(encoding="utf-8")
    assert "_install_turn_pipeline()" in boot
    assert "relationship_growth_runtime" not in boot
    assert "relationship_policy_runtime" not in boot


def test_runtime_authoring_documents_remain_separate_from_backend_modules():
    from app import runtime_access
    assert runtime_access.AUTHOR_RUNTIME_FILES == ("rules.md", "scene_builder.md")
    assert set(runtime_access.runtime_documents()) == {"rules", "scene_builder"}
