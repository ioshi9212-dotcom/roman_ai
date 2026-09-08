from __future__ import annotations

import re
from typing import Any, List


SCENE_DIVIDER = "--------------------------------------------------------"
OPTION_MARKERS = ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:")
TURN_RE = re.compile(r"^Ход\s+\d+\s*·\s*цикл\s+\d+/15\s*$", re.MULTILINE)
NUMBERED_RE = re.compile(r"^\s*([1-3])\.\s+\S.*$", re.MULTILINE)
REQUIRED_PREFIXES = ("🎭 ", "🕒 День ", "🌦️ Погода:", "⚙️ Сцена:", "✦ ", "🧥 Одежда, волосы:")


def _first_line_index(lines: List[str], prefix: str) -> int:
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            return index
    return -1


def validate_scene_output(scene_output: Any) -> str:
    """Validate the public scene-builder contract and return normalized text."""
    text = str(scene_output or "").strip()
    if not text:
        raise ValueError("SCENE_FORMAT_INVALID:empty_scene")

    lines = [line.rstrip() for line in text.splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    errors: List[str] = []

    header_indices = [_first_line_index(lines, prefix) for prefix in REQUIRED_PREFIXES]
    if not nonempty or not nonempty[0].startswith("🎭 "):
        errors.append("title_header")
    for prefix, index in zip(REQUIRED_PREFIXES, header_indices):
        if index < 0:
            errors.append(prefix.strip())
    if all(index >= 0 for index in header_indices) and header_indices != sorted(header_indices):
        errors.append("header_order")

    divider_pos = text.find(SCENE_DIVIDER)
    if divider_pos < 0:
        errors.append("divider")
    elif all(index >= 0 for index in header_indices):
        divider_line = next((index for index, line in enumerate(lines) if line.strip() == SCENE_DIVIDER), -1)
        if divider_line < max(header_indices):
            errors.append("header_order")

    positions = [text.find(marker) for marker in OPTION_MARKERS]
    if any(pos < 0 for pos in positions):
        errors.append("option_sections")
    elif positions != sorted(positions):
        errors.append("option_order")
    else:
        if divider_pos >= 0 and positions[0] < divider_pos:
            errors.append("option_order")
        tails = positions[1:] + [len(text)]
        for marker, start, end in zip(OPTION_MARKERS, positions, tails):
            section = text[start + len(marker):end]
            nums = [int(match.group(1)) for match in NUMBERED_RE.finditer(section)]
            if nums != [1, 2, 3]:
                errors.append(f"{marker}exactly_3")

    state_pos = text.find("Состояние:")
    relationships_pos = text.find("Отношения:")
    turn_matches = list(TURN_RE.finditer(text))
    turn_match = turn_matches[-1] if turn_matches else None

    if state_pos < 0:
        errors.append("state_footer")
    if relationships_pos < 0:
        errors.append("relationships_footer")
    if not turn_match:
        errors.append("turn_footer")
    if positions[2] >= 0 and state_pos >= 0 and positions[2] > state_pos:
        errors.append("footer_order")
    if state_pos >= 0 and relationships_pos >= 0 and state_pos > relationships_pos:
        errors.append("footer_order")
    if relationships_pos >= 0 and turn_match and relationships_pos > turn_match.start():
        errors.append("footer_order")
    if turn_match and nonempty and nonempty[-1] != turn_match.group(0).strip():
        errors.append("content_after_turn_footer")
    if len(turn_matches) != 1:
        errors.append("turn_footer_count")

    if errors:
        raise ValueError("SCENE_FORMAT_INVALID:" + ",".join(dict.fromkeys(errors)))
    return text
