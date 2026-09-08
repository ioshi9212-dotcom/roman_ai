from __future__ import annotations

import re
from typing import Any, List


SCENE_DIVIDER = "--------------------------------------------------------"
OPTION_MARKERS = ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:")
TURN_RE = re.compile(r"^Ход\s+\d+\s*·\s*цикл\s+\d+/15\s*$", re.MULTILINE)
NUMBERED_RE = re.compile(r"^\s*([1-3])\.\s+\S.*$", re.MULTILINE)
REQUIRED_PREFIXES = ("🎭 ", "🕒 День ", "🌦️ Погода:", "⚙️ Сцена:", "✦ ", "🧥 Одежда, волосы:")


def validate_scene_output(scene_output: Any) -> str:
    """Validate the public scene-builder contract and return the normalized text."""
    text = str(scene_output or "").strip()
    if not text:
        raise ValueError("SCENE_FORMAT_INVALID:empty_scene")

    lines = [line.rstrip() for line in text.splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    errors: List[str] = []

    if not nonempty or not nonempty[0].startswith("🎭 "):
        errors.append("title_header")
    for prefix in REQUIRED_PREFIXES:
        if not any(line.strip().startswith(prefix) for line in lines):
            errors.append(prefix.strip())
    if SCENE_DIVIDER not in text:
        errors.append("divider")

    positions = [text.find(marker) for marker in OPTION_MARKERS]
    if any(pos < 0 for pos in positions):
        errors.append("option_sections")
    elif positions != sorted(positions):
        errors.append("option_order")
    else:
        tails = positions[1:] + [len(text)]
        for marker, start, end in zip(OPTION_MARKERS, positions, tails):
            section = text[start + len(marker):end]
            nums = [int(match.group(1)) for match in NUMBERED_RE.finditer(section)]
            if nums != [1, 2, 3]:
                errors.append(f"{marker}exactly_3")

    state_pos = text.find("Состояние:")
    relationships_pos = text.find("Отношения:")
    turn_match = TURN_RE.search(text)
    if state_pos < 0:
        errors.append("state_footer")
    if relationships_pos < 0:
        errors.append("relationships_footer")
    if not turn_match:
        errors.append("turn_footer")
    if state_pos >= 0 and relationships_pos >= 0 and state_pos > relationships_pos:
        errors.append("footer_order")
    if relationships_pos >= 0 and turn_match and relationships_pos > turn_match.start():
        errors.append("footer_order")

    if errors:
        raise ValueError("SCENE_FORMAT_INVALID:" + ",".join(dict.fromkeys(errors)))
    return text
