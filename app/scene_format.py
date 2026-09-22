from __future__ import annotations

import re
from typing import Any, List


SCENE_DIVIDER = "--------------------------------------------------------"
OPTION_MARKERS = ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:")
TURN_RE = re.compile(r"^Ход\s+\d+\s*·\s*цикл\s+\d+/15\s*$", re.MULTILINE)
NUMBERED_RE = re.compile(r"^\s*([1-3])\.\s+\S.*$", re.MULTILINE)
OPTION_LINE_RE = re.compile(r"^\s*(Что я могу (?:сделать|сказать|подумать):)\s*$")
STATE_LINE_RE = re.compile(r"^\s*Состояние:\s*\S.*$")
RELATIONSHIPS_LINE_RE = re.compile(r"^\s*Отношения:\s*$")
REQUIRED_PREFIXES = ("🎭 ", "🕒 День ", "🌦️ Погода:", "⚙️ Сцена:", "✦ ", "🧥 Одежда, волосы:")


INLINE_HEADER_PREFIXES = {"🌦️ Погода:", "🧥 Одежда, волосы:"}


def _first_line_index(lines: List[str], prefix: str) -> int:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if prefix in INLINE_HEADER_PREFIXES:
            if prefix in stripped:
                return index
        elif stripped.startswith(prefix):
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

    marker_lines: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = OPTION_LINE_RE.match(line)
        if not match:
            continue
        marker = match.group(1)
        if marker in marker_lines:
            errors.append("option_sections_duplicate")
        marker_lines.setdefault(marker, index)

    if any(marker not in marker_lines for marker in OPTION_MARKERS):
        errors.append("option_sections")
        positions = [text.find(marker) for marker in OPTION_MARKERS]
    else:
        option_line_indices = [marker_lines[marker] for marker in OPTION_MARKERS]
        if option_line_indices != sorted(option_line_indices):
            errors.append("option_order")
        if divider_pos >= 0:
            divider_line = next((index for index, line in enumerate(lines) if line.strip() == SCENE_DIVIDER), -1)
            if divider_line >= 0 and option_line_indices[0] <= divider_line:
                errors.append("option_order")

        state_line = next((index for index, line in enumerate(lines) if line.strip().startswith("Состояние:")), -1)
        section_ends = [option_line_indices[1], option_line_indices[2], state_line if state_line >= 0 else len(lines)]
        for marker, start_line, end_line in zip(OPTION_MARKERS, option_line_indices, section_ends):
            body = [line.strip() for line in lines[start_line + 1:end_line] if line.strip()]
            nums = []
            for row in body:
                match = NUMBERED_RE.fullmatch(row)
                if match:
                    nums.append(int(match.group(1)))
                else:
                    errors.append(f"{marker}foreign_content")
            if nums != [1, 2, 3] or len(body) != 3:
                errors.append(f"{marker}exactly_3")

        positions = [text.find(marker) for marker in OPTION_MARKERS]

    state_pos = text.find("Состояние:")
    relationships_pos = text.find("Отношения:")
    turn_matches = list(TURN_RE.finditer(text))
    turn_match = turn_matches[-1] if turn_matches else None

    state_lines = [index for index, line in enumerate(lines) if line.strip().startswith("Состояние:")]
    relationship_lines = [index for index, line in enumerate(lines) if line.strip() == "Отношения:"]
    if state_pos < 0 or len(state_lines) != 1 or not STATE_LINE_RE.match(lines[state_lines[0]] if state_lines else ""):
        errors.append("state_footer")
    if relationships_pos < 0 or len(relationship_lines) != 1 or not RELATIONSHIPS_LINE_RE.match(lines[relationship_lines[0]] if relationship_lines else ""):
        errors.append("relationships_footer")
    if state_lines and marker_lines.get(OPTION_MARKERS[2]) is not None:
        thought_start = marker_lines[OPTION_MARKERS[2]]
        thought_body = [line.strip() for line in lines[thought_start + 1:state_lines[0]] if line.strip()]
        if len(thought_body) != 3:
            errors.append("lower_block_boundary")
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
