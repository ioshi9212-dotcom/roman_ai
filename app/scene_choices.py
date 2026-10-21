"""Presentation data only: read choices from a committed scene, never advance play."""
import re
from hashlib import sha256
from uuid import UUID

from . import storage

GROUPS = (("action", "Что я могу сделать"), ("speech", "Что я могу сказать"),
          ("thought", "Что я могу подумать"))
_MARKER = re.compile(r"^\s*(?:\*\*)?(Что я могу (?:сделать|сказать|подумать)):(?:\*\*)?\s*$", re.MULTILINE)
_ITEM = re.compile(r"^[ \t]*(?:[1-3][.)]|[-*]\s*\[[ xX]\])[ \t]+", re.MULTILINE)


def parse_choices(scene: str) -> list[dict]:
    markers = list(_MARKER.finditer(scene))
    if [m[1] for m in markers] != [title for _, title in GROUPS]:
        raise ValueError("SCENE_CHOICES_INCOMPLETE")
    groups = []
    for i, (kind, title) in enumerate(GROUPS):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(scene)
        block = scene[markers[i].end():end].split("Состояние:", 1)[0]
        starts = list(_ITEM.finditer(block))
        items = [block[m.end():starts[j + 1].start() if j + 1 < len(starts) else len(block)].strip()
                 for j, m in enumerate(starts)]
        if len(items) != 3 or any(not item.strip() or item.strip() == "..." for item in items):
            raise ValueError("SCENE_CHOICES_INCOMPLETE")
        groups.append({"kind": kind, "title": title,
                       "items": [{"id": f"{kind}-{j}", "text": text.strip()}
                                 for j, text in enumerate(items)]})
    return groups


def saved_scene_choices(session_id: str, expected_turn: int) -> dict:
    # A UUID is the existing session capability. Never accept filesystem paths.
    parsed = UUID(session_id)
    if session_id not in (str(parsed), parsed.hex) or expected_turn < 1:
        raise ValueError("INVALID_SESSION_OR_TURN")
    root = storage.SESSIONS_DIR / session_id
    if not root.is_dir():
        raise FileNotFoundError("SESSION_NOT_FOUND")
    turns = storage._read_turns_backwards(root, count=1)
    if not turns:
        raise ValueError("NO_COMMITTED_SCENE")
    turn = turns[-1]
    if int(turn["turn_number"]) != expected_turn:
        raise ValueError("SCENE_CHANGED_REOPEN_CHOICES")
    return {"session_id": session_id, "turn_number": expected_turn,
            "scene_sha256": sha256((str(turn.get("turn_id") or "") + "\0" + turn["scene_output"]).encode()).hexdigest(),
            "groups": parse_choices(turn["scene_output"])}
