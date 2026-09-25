from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import storage
from .scene_format import SCENE_DIVIDER


RUNTIME_CONTRACT_VERSION = 1
OPTION_HEADERS = ("Что я могу сделать:", "Что я могу сказать:", "Что я могу подумать:")

_TITLE_RE = re.compile(r"^🎭\s+(.+?)\s+·\s+(.+?)\s*$")
_CLOCK_RE = re.compile(
    r"^🕒\s+День\s+(\d+)\s+·\s+([^,]+),\s+(\d{2}\.\d{2}\.\d{4}),\s+(\d{2}:\d{2})\s+·\s*$"
)
_LOCATION_WEATHER_RE = re.compile(r"^📍\s+(.+?)\s+🌦️\s+Погода:\s+(.+?)\s*$")
_SCENE_RE = re.compile(r"^⚙️\s+Сцена:\s+(.+?)\s*$")
_POV_RE = re.compile(
    r"^✦\s+(.+?)\s+🧥\s+Одежда, волосы:\s+(.+?)\s+◈\s+Инвентарь:\s+(.+?)\s*$"
)
_OPTION_RE = re.compile(r"^([1-3])\.\s+\S.*$")
_TURN_RE = re.compile(r"^Ход\s+(\d+)\s+·\s+цикл\s+(\d+)/15\s*$")
_RELATIONSHIP_RE = re.compile(
    r"^.+?\s+-\s+[\wА-Яа-яЁё-]+\s+-?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?"
    r"(?:;\s*[\wА-Яа-яЁё-]+\s+-?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?)*\s*$"
)
_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")


class RuntimeContractError(RuntimeError):
    pass


def contract_packet() -> Dict[str, Any]:
    return {
        "version": RUNTIME_CONTRACT_VERSION,
        "mandatory": True,
        "documents": ["runtime_rules", "scene_builder"],
        "commit_review_fields": [
            "runtime_rules_reviewed",
            "scene_builder_reviewed",
            "runtime_contract_version",
        ],
        "hard_checks": [
            "exact_scene_builder_structure",
            "main_scene_max_3000_chars",
            "fixed_novel_title",
            "exact_turn_and_cycle_footer",
            "player_spoken_segments_preserved_in_order",
            "state_max_10_words",
            "relationship_footer_shape",
        ],
        "instruction": (
            "runtime_rules and scene_builder are hard contracts, not suggestions. Read both completely before writing. "
            "Before commit, rewrite any mismatch; then set both review flags true and echo this version."
        ),
    }


def _nonempty_lines(text: str) -> List[Tuple[int, str]]:
    return [(index, line.strip()) for index, line in enumerate(text.splitlines()) if line.strip()]


def _parse_player_input(text: str) -> List[str]:
    ordered: List[Tuple[str, str]] = []
    buffer: List[str] = []
    depth = 0

    def flush(kind: str) -> None:
        value = "".join(buffer).strip()
        buffer.clear()
        if value:
            ordered.append((kind, value))

    for char in str(text or ""):
        if char == "(":
            if depth == 0:
                flush("spoken")
                depth = 1
                continue
            depth += 1
            buffer.append(char)
        elif char == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                flush("stage_direction")
            else:
                buffer.append(char)
        else:
            buffer.append(char)
    flush("stage_direction" if depth else "spoken")
    return [value for kind, value in ordered if kind == "spoken"]


def _norm_words(text: str) -> str:
    return " ".join(_WORD_RE.findall(str(text or "").casefold()))


def _source_title(root: Path) -> str:
    source = storage._read_json(root / "source.json", {})
    return str(source.get("title") or "").strip() if isinstance(source, dict) else ""


def _pov_name(root: Path) -> str:
    source = storage._read_json(root / "source.json", {})
    state = storage._read_json(root / "state.json", {})
    cards = storage._load_cards(root, source if isinstance(source, dict) else {})

    pov_id = ""
    if isinstance(state, dict):
        pov = state.get("pov")
        if isinstance(pov, dict):
            pov_id = str(pov.get("character_id") or pov.get("id") or "").strip()
    if not pov_id and isinstance(source, dict):
        novel = source.get("novel")
        if isinstance(novel, dict):
            pov_id = str(novel.get("pov_character") or novel.get("pov_character_id") or "").strip()

    if pov_id:
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = str(card.get("character_id") or card.get("id") or "").strip()
            if cid == pov_id:
                return str(card.get("name") or card.get("full_name") or "").strip()
    for card in cards:
        if isinstance(card, dict) and card.get("is_pov") is True:
            return str(card.get("name") or card.get("full_name") or "").strip()
    return ""


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(str(text or "")))


def _strict_scene_parts(scene_output: str) -> Dict[str, Any]:
    text = str(scene_output or "").strip()
    lines = text.splitlines()
    nonempty = _nonempty_lines(text)
    if len(nonempty) < 10:
        raise RuntimeContractError("SCENE_BUILDER_STRUCTURE_INVALID")

    # Exact top block from the current scene_builder.
    header = [line for _, line in nonempty[:5]]
    title_match = _TITLE_RE.fullmatch(header[0])
    clock_match = _CLOCK_RE.fullmatch(header[1])
    location_match = _LOCATION_WEATHER_RE.fullmatch(header[2])
    scene_match = _SCENE_RE.fullmatch(header[3])
    pov_match = _POV_RE.fullmatch(header[4])
    if not all((title_match, clock_match, location_match, scene_match, pov_match)):
        raise RuntimeContractError("SCENE_BUILDER_HEADER_INVALID")
    if _word_count(scene_match.group(1)) > 10:
        raise RuntimeContractError("SCENE_BUILDER_SCENE_LABEL_TOO_LONG")

    divider_lines = [index for index, line in enumerate(lines) if line.strip() == SCENE_DIVIDER]
    if len(divider_lines) != 1:
        raise RuntimeContractError("SCENE_BUILDER_DIVIDER_INVALID")
    divider = divider_lines[0]
    if divider <= nonempty[4][0]:
        raise RuntimeContractError("SCENE_BUILDER_HEADER_INVALID")

    option_indices: Dict[str, int] = {}
    for marker in OPTION_HEADERS:
        found = [index for index, line in enumerate(lines) if line.strip() == marker]
        if len(found) != 1:
            raise RuntimeContractError("SCENE_BUILDER_OPTIONS_INVALID")
        option_indices[marker] = found[0]
    action_i, say_i, thought_i = [option_indices[marker] for marker in OPTION_HEADERS]
    if not (divider < action_i < say_i < thought_i):
        raise RuntimeContractError("SCENE_BUILDER_OPTIONS_INVALID")

    main_scene = "\n".join(lines[divider + 1:action_i]).strip()
    if not main_scene or len(main_scene) > 3000:
        raise RuntimeContractError("SCENE_BUILDER_MAIN_LENGTH_INVALID")

    state_rows = [index for index, line in enumerate(lines) if line.strip().startswith("Состояние:")]
    rel_rows = [index for index, line in enumerate(lines) if line.strip() == "Отношения:"]
    turn_rows = [(index, _TURN_RE.fullmatch(line.strip())) for index, line in enumerate(lines)]
    turn_rows = [(index, match) for index, match in turn_rows if match]
    if len(state_rows) != 1 or len(rel_rows) != 1 or len(turn_rows) != 1:
        raise RuntimeContractError("SCENE_BUILDER_FOOTER_INVALID")
    state_i = state_rows[0]
    rel_i = rel_rows[0]
    turn_i, turn_match = turn_rows[0]
    if not (thought_i < state_i < rel_i < turn_i):
        raise RuntimeContractError("SCENE_BUILDER_FOOTER_INVALID")

    def exact_options(start: int, end: int) -> List[str]:
        rows = [line.strip() for line in lines[start + 1:end] if line.strip()]
        if len(rows) != 3:
            raise RuntimeContractError("SCENE_BUILDER_OPTIONS_INVALID")
        numbers: List[int] = []
        for row in rows:
            match = _OPTION_RE.fullmatch(row)
            if match is None:
                raise RuntimeContractError("SCENE_BUILDER_OPTIONS_INVALID")
            numbers.append(int(match.group(1)))
        if numbers != [1, 2, 3]:
            raise RuntimeContractError("SCENE_BUILDER_OPTIONS_INVALID")
        return rows

    actions = exact_options(action_i, say_i)
    says = exact_options(say_i, thought_i)
    thoughts = exact_options(thought_i, state_i)

    state_line = lines[state_i].strip()
    if not state_line.startswith("Состояние: "):
        raise RuntimeContractError("SCENE_BUILDER_STATE_INVALID")
    state_value = state_line.split(":", 1)[1].strip()
    if not state_value or _word_count(state_value) > 10:
        raise RuntimeContractError("SCENE_BUILDER_STATE_INVALID")

    # No free prose between state and relationship footer.
    between_state_rel = [line.strip() for line in lines[state_i + 1:rel_i] if line.strip()]
    if between_state_rel:
        raise RuntimeContractError("SCENE_BUILDER_FOOTER_INVALID")

    relationship_rows = [line.strip() for line in lines[rel_i + 1:turn_i] if line.strip()]
    for row in relationship_rows:
        if not _RELATIONSHIP_RE.fullmatch(row):
            raise RuntimeContractError("SCENE_BUILDER_RELATIONSHIP_FORMAT_INVALID")

    final_nonempty = nonempty[-1][0]
    if final_nonempty != turn_i:
        raise RuntimeContractError("SCENE_BUILDER_CONTENT_AFTER_FOOTER")

    return {
        "title": title_match.group(1).strip(),
        "season": title_match.group(2).strip(),
        "game_day": int(clock_match.group(1)),
        "date": clock_match.group(3),
        "time": clock_match.group(4),
        "location": location_match.group(1).strip(),
        "weather": location_match.group(2).strip(),
        "pov_name": pov_match.group(1).strip(),
        "main_scene": main_scene,
        "actions": actions,
        "says": says,
        "thoughts": thoughts,
        "state": state_value,
        "relationship_rows": relationship_rows,
        "turn_number": int(turn_match.group(1)),
        "cycle_position": int(turn_match.group(2)),
    }


def validate_runtime_contract(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    if extracted.get("runtime_rules_reviewed") is not True:
        raise RuntimeContractError("RUNTIME_RULES_REVIEW_REQUIRED")
    if extracted.get("scene_builder_reviewed") is not True:
        raise RuntimeContractError("SCENE_BUILDER_REVIEW_REQUIRED")
    try:
        version = int(extracted.get("runtime_contract_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version != RUNTIME_CONTRACT_VERSION:
        raise RuntimeContractError("RUNTIME_CONTRACT_VERSION_MISMATCH")

    parts = _strict_scene_parts(str(payload.get("scene_output") or ""))

    expected_title = _source_title(root)
    if expected_title and parts["title"] != expected_title:
        raise RuntimeContractError("SCENE_BUILDER_TITLE_MISMATCH")

    expected_pov = _pov_name(root)
    if expected_pov and parts["pov_name"] != expected_pov:
        raise RuntimeContractError("SCENE_BUILDER_POV_MISMATCH")

    meta = storage._read_json(root / "meta.json", {})
    committed_turn = int(meta.get("turn_number", 0) or 0) if isinstance(meta, dict) else 0
    expected_turn = committed_turn + 1
    expected_cycle = ((expected_turn - 1) % 15) + 1
    if parts["turn_number"] != expected_turn or parts["cycle_position"] != expected_cycle:
        raise RuntimeContractError("SCENE_BUILDER_TURN_FOOTER_MISMATCH")

    raw_input = str(payload.get("user_input") or "")
    is_launch_control = (
        committed_turn == 0
        and _norm_words(raw_input) == _norm_words("запускай первую сцену")
    )
    if not is_launch_control:
        main_norm = _norm_words(parts["main_scene"])
        search_from = 0
        for spoken in _parse_player_input(raw_input):
            spoken_norm = _norm_words(spoken)
            if not spoken_norm:
                continue
            position = main_norm.find(spoken_norm, search_from)
            if position < 0:
                if spoken_norm in main_norm:
                    raise RuntimeContractError("RUNTIME_RULE_PLAYER_SPEECH_ORDER_INVALID")
                raise RuntimeContractError("RUNTIME_RULE_PLAYER_SPEECH_NOT_PRESERVED")
            search_from = position + len(spoken_norm)
