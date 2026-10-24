from __future__ import annotations

import re
from typing import Any, List


# Only the *presentation grammar* is checked here. Narrative choices and
# pacing are owned by runtime/scene_builder.md, not by the validator.
SCENE_DIVIDER = "--------------------------------------------------------"
OPTION_MARKERS = ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:")
TURN_RE = re.compile(r"^Ход\s+\d+\s*·\s*цикл\s+\d+/15\s*$", re.MULTILINE)
NUMBERED_RE = re.compile(r"^\s*([1-3])\.\s+\S.*$", re.MULTILINE)
LEGACY_PREFIXES = ("🎭 ", "🕒 День ", "🌦️ Погода:", "⚙️ Сцена:", "✦ ", "🧥 Одежда, волосы:")
CURRENT_PREFIXES = ("**◈ ", "◷ День ", "⌖ ", "▸ ", "◇ **", "▣ При себе:")
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


def _header_errors(lines: List[str]) -> List[str]:
    nonempty = [line.strip() for line in lines if line.strip()]
    errors: List[str] = []
    if not nonempty:
        return ["title_header"]
    first = nonempty[0]
    # Older saved scenes must continue to pass revision/rollback.
    if first.startswith("🎭 "):
        prefixes = LEGACY_PREFIXES
    elif first.startswith("**◈ "):
        prefixes = CURRENT_PREFIXES
    else:
        return ["title_header"]

    positions = [_first_line_index(lines, prefix) for prefix in prefixes]
    for prefix, index in zip(prefixes, positions):
        if index < 0:
            errors.append(prefix.strip())
    if all(index >= 0 for index in positions) and positions != sorted(positions):
        errors.append("header_order")
    return errors


def validate_scene_output(scene_output: Any) -> str:
    """Check formatting of both published builder headers, never story content."""
    text = str(scene_output or "").strip()
    if not text:
        raise ValueError("SCENE_FORMAT_INVALID:empty_scene")

    lines = [line.rstrip() for line in text.splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    errors: List[str] = _header_errors(lines)

    # The current minimalist builder uses a fine line and a dotted separator;
    # the older builder used the 56-hyphen line. Both are just separators.
    divider = re.search(r"(?m)^(?:-{20,}|─{8,})\s*$", text)
    if divider is None:
        errors.append("divider")
    else:
        divider_line = text[:divider.start()].count("\n")
        if any(_first_line_index(lines, prefix) > divider_line for prefix in (
            CURRENT_PREFIXES if nonempty and nonempty[0].startswith("**◈ ") else LEGACY_PREFIXES
        ) if _first_line_index(lines, prefix) >= 0):
            errors.append("header_order")

    positions = [text.find(marker) for marker in OPTION_MARKERS]
    if any(pos < 0 for pos in positions):
        errors.append("option_sections")
    elif positions != sorted(positions):
        errors.append("option_order")
    else:
        if divider is not None and positions[0] < divider.start():
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
