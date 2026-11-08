"""Select verbatim pair-specific author reaction rules from archived RAW intake.

RAW is director context, not a character's memory or proof of hidden lore.
"""
from __future__ import annotations

import re
from typing import Any

_REACTION = re.compile(
    r"^\s*Реакция\s+(?P<actor>[^\s:]+)\s+на\s+(?P<target>[^\s:]+)\s*:\s*(?P<detail>.+?)\s*$",
    re.IGNORECASE,
)


def _normal(value: Any) -> str:
    return re.sub(r"[^a-zа-я]", "", str(value or "").casefold().replace("ё", "е"))


def _resolve_case_name(name: str, cards: list[dict[str, Any]]) -> str | None:
    """Resolve declined Russian first names uniquely, without guessing identities."""
    word = _normal(name)
    matches: list[str] = []
    for card in cards:
        cid = str(card.get("character_id") or card.get("id") or "")
        first = str(card.get("name") or card.get("full_name") or "").split(" ", 1)[0]
        canonical = _normal(first)
        if not cid or len(canonical) < 4:
            continue
        stem = canonical[:min(5, len(canonical))]
        if word == canonical or (len(word) >= len(stem) and word.startswith(stem)):
            matches.append(cid)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def scene_reactions(
    source: dict[str, Any],
    cards: list[dict[str, Any]],
    participant_ids: list[str],
) -> list[dict[str, str]]:
    """Extract exact RAW reaction lines for active pairs only."""
    available = {str(value) for value in participant_ids if value}
    intake = source.get("source_intake")
    if not isinstance(intake, list) or len(available) < 2:
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for block in intake:
        raw = block.get("raw_text") if isinstance(block, dict) else None
        if not isinstance(raw, str):
            continue
        for line in raw.splitlines():
            match = _REACTION.match(line)
            if not match:
                continue
            actor = _resolve_case_name(match.group("actor"), cards)
            target = _resolve_case_name(match.group("target"), cards)
            if not actor or not target or actor == target or actor not in available or target not in available:
                continue
            literal = line.strip()
            key = (actor, target, literal)
            if key not in seen:
                seen.add(key)
                result.append({
                    "from_character_id": actor,
                    "toward_character_id": target,
                    "verbatim_author_rule": literal,
                })
    return result
