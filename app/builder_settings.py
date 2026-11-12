"""Per-session Skin Builder selection, separate from story and rollback state."""
from __future__ import annotations

import re
from pathlib import Path

from . import runtime_access, storage

SETTINGS_FILE = ".runtime/builder_settings.json"


def stored_builder(root: Path) -> str | None:
    settings = storage._read_json(root / SETTINGS_FILE, {})
    value = settings.get("builder_version") if isinstance(settings, dict) else None
    return runtime_access.normalize_builder_version(value) if value is not None else None


def infer_builder(root: Path, pending: dict | None = None) -> str:
    """Recover an existing session's choice without scanning or changing history."""
    value = stored_builder(root)
    if value is not None:
        return value
    if pending is None:
        pending = storage._read_json(root / "turn_packet.json", {})
    if isinstance(pending, dict) and pending.get("packet_id"):
        return runtime_access.normalize_builder_version(pending.get("builder_version"))
    turns = storage._read_turns_backwards(root, count=1)
    if turns:
        lines = str(turns[-1].get("scene_output") or "").lstrip().splitlines()
        title = lines[0] if lines else ""
        # The already committed title is a fallback only for pre-settings sessions.
        if title.startswith("**◈ ") and re.search(r"·\s*V2(?:\*\*|\s|$)", title):
            return "v2"
        if title.startswith("📖 ") and re.search(r"·\s*V3(?:\*\*|\s|$)", title):
            return "v3"
    return "orig"


def save_builder(root: Path, version: str) -> str:
    """Call under session_transaction; no gameplay turn or canon mutation."""
    selected = runtime_access.normalize_builder_version(version)
    if stored_builder(root) != selected:
        storage._write_json(root / SETTINGS_FILE, {"builder_version": selected})
    return selected
