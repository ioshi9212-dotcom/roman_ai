from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from . import storage
from .scene_format import OPTION_MARKERS, SCENE_DIVIDER


_SPEAKER_RE = re.compile(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*[—-]\s*")


def _normalise_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split()).strip(" .,:;!?—-")


def _pov_aliases(root) -> set[str]:
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or storage._find_pov_id(source, cards) or "")
    aliases: set[str] = set()
    for card in cards:
        if storage._card_id(card) != pov_id:
            continue
        for name in storage._card_names(card):
            normalized = _normalise_name(name)
            if normalized:
                aliases.add(normalized)
        break
    return aliases


def _main_scene_body(scene_output: str) -> str:
    text = str(scene_output or "")
    divider = text.find(SCENE_DIVIDER)
    if divider < 0:
        return ""
    start = divider + len(SCENE_DIVIDER)
    option_positions = [pos for marker in OPTION_MARKERS if (pos := text.find(marker, start)) >= 0]
    end = min(option_positions) if option_positions else len(text)
    return text[start:end].strip()


def _speaker_positions(body: str, pov_aliases: set[str]) -> tuple[List[int], List[int]]:
    pov: List[int] = []
    npc: List[int] = []
    for match in _SPEAKER_RE.finditer(body):
        speaker = _normalise_name(match.group("speaker"))
        if speaker in pov_aliases:
            pov.append(match.start())
        else:
            npc.append(match.start())
    return pov, npc


def _alias_positions(body: str, aliases: Iterable[str]) -> List[int]:
    lowered = body.casefold().replace("ё", "е")
    positions: List[int] = []
    for alias in aliases:
        if not alias:
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
        positions.extend(match.start() for match in pattern.finditer(lowered))
    return positions


def validate_pov_participation(session_id: str, scene_output: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    body = _main_scene_body(scene_output)
    aliases = _pov_aliases(root)
    if not body or not aliases:
        return {"checked": False, "reason": "missing_body_or_pov_alias"}

    pov_dialogue, npc_dialogue = _speaker_positions(body, aliases)
    if len(npc_dialogue) < 2:
        return {
            "checked": True,
            "npc_dialogue_lines": len(npc_dialogue),
            "pov_dialogue_lines": len(pov_dialogue),
            "furniture_risk": False,
        }

    evidence = sorted(set(pov_dialogue + _alias_positions(body, aliases)))
    if not evidence:
        raise RuntimeError("POV_PARTICIPATION_REQUIRED")

    last_pov = evidence[-1]
    npc_after = sum(1 for pos in npc_dialogue if pos > last_pov)
    trailing_chars = max(0, len(body) - last_pov)

    # Conservative enforcement: reject only obvious cases where the scene keeps
    # running through several NPC beats after POV has effectively vanished.
    if npc_after >= 3 and trailing_chars >= 500:
        raise RuntimeError("POV_PARTICIPATION_REQUIRED")
    if npc_after >= 2 and last_pov < int(len(body) * 0.35) and trailing_chars >= 900:
        raise RuntimeError("POV_PARTICIPATION_REQUIRED")

    return {
        "checked": True,
        "npc_dialogue_lines": len(npc_dialogue),
        "pov_dialogue_lines": len(pov_dialogue),
        "last_pov_evidence": last_pov,
        "npc_dialogue_after_last_pov": npc_after,
        "furniture_risk": False,
    }


__all__ = ["validate_pov_participation"]
