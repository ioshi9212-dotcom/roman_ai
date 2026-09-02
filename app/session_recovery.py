from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

from . import storage


_SCENE_HEADER_RE = re.compile(
    r"🕒\s*День\s+\d+\s*·[^\n]*?(?P<date>\d{2}\.\d{2}\.\d{4}),\s*"
    r"(?P<time>\d{1,2}:\d{2})\s*·\s*📍\s*(?P<location>[^\n]+)"
)
_SCENE_NAME_RE = re.compile(r"^⚙️\s*Сцена:\s*(?P<scene>.+?)\s*$", re.MULTILINE)
_RELATIONSHIP_LINE_RE = re.compile(r"^(.+?)\s+-\s+.+$", re.MULTILINE)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _resolve_character_id(cards: Iterable[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    if not value:
        return None
    needle = _norm(value)
    for card in cards:
        cid = storage._card_id(card)
        if _norm(cid) == needle:
            return cid
        if any(_norm(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _normalise_present(cards: Iterable[Dict[str, Any]], value: Any) -> List[str]:
    if isinstance(value, dict):
        values = list(value.keys())
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: List[str] = []
    for raw in values:
        resolved = _resolve_character_id(cards, raw)
        if resolved:
            result.append(resolved)
            continue
        if isinstance(raw, dict):
            raw = raw.get("character_id") or raw.get("id") or raw.get("name")
        if raw:
            result.append(str(raw))
    return list(dict.fromkeys(result))


def _merge_current(
    current: Dict[str, Any],
    patch: Any,
    *,
    cards: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    result = deepcopy(current if isinstance(current, dict) else {})
    if not isinstance(patch, dict):
        return result
    for key, value in patch.items():
        if key == "present_characters":
            present = _normalise_present(cards, value)
            if present:
                result[key] = present
            continue
        if _nonempty(value):
            result[key] = deepcopy(value)
    return result


def _parse_scene_header(scene_output: Any) -> Dict[str, Any]:
    if not isinstance(scene_output, str) or not scene_output.strip():
        return {}
    result: Dict[str, Any] = {}
    match = _SCENE_HEADER_RE.search(scene_output)
    if match:
        result["date"] = match.group("date").strip()
        result["time"] = match.group("time").strip()
        result["location"] = match.group("location").strip()
    scene_match = _SCENE_NAME_RE.search(scene_output)
    if scene_match:
        result["scene"] = scene_match.group("scene").strip()
    return result


def _footer_present_ids(
    scene_output: Any,
    *,
    cards: Iterable[Dict[str, Any]],
) -> List[str]:
    if not isinstance(scene_output, str):
        return []
    lines = scene_output.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "Отношения:") + 1
    except StopIteration:
        return []
    result: List[str] = []
    for raw in lines[start:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Ход "):
            break
        match = _RELATIONSHIP_LINE_RE.match(line)
        if not match:
            continue
        resolved = _resolve_character_id(cards, match.group(1).strip())
        if resolved:
            result.append(resolved)
    return list(dict.fromkeys(result))


def _event_participants(events: Any, *, cards: Iterable[Dict[str, Any]]) -> List[str]:
    if not isinstance(events, list):
        return []
    result: List[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        raw = event.get("participants_present") or event.get("participants") or event.get("character_ids") or []
        result.extend(_normalise_present(cards, raw))
    return list(dict.fromkeys(result))


def _timestamp(value: Any, fallback: int) -> Tuple[str, int]:
    text = str(value or "").strip()
    return (text, fallback)


def _pov_id(state: Dict[str, Any], source: Dict[str, Any], cards: List[Dict[str, Any]]) -> str:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    return str(pov.get("character_id") or storage._find_pov_id(source, cards) or "")


def current_recovery_status(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    current = state.get("current") if isinstance(state.get("current"), dict) else None
    pov_id = _pov_id(state, source, cards)
    reasons: List[str] = []

    if current is None:
        reasons.append("current_not_object")
        present: List[str] = []
    elif not current:
        reasons.append("current_empty")
        present = []
    else:
        pointer_values = (
            current.get("date") or current.get("game_date") or current.get("calendar_date"),
            current.get("time") or current.get("game_time"),
            current.get("location") or current.get("place") or current.get("area"),
        )
        if not any(_nonempty(value) for value in pointer_values):
            reasons.append("current_scene_pointer_missing")
        present = _normalise_present(cards, current.get("present_characters"))

    # A scene pointer with date/location but no cast is still broken. This was the hole that
    # let recovery declare damaged sessions healthy after only the header had been rebuilt.
    if not present:
        reasons.append("present_characters_empty")
    if pov_id and pov_id not in present:
        reasons.append("pov_missing_from_present")

    return {
        "required": bool(reasons),
        "reasons": list(dict.fromkeys(reasons)),
        "current": deepcopy(current) if isinstance(current, dict) else {},
        "present_character_ids": present,
        "pov_character_id": pov_id or None,
    }


def _reconstruct_current(root, source: Dict[str, Any], cards: List[Dict[str, Any]]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    starting_state = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    starting_current = starting_state.get("current") if isinstance(starting_state.get("current"), dict) else {}
    recovered = _merge_current({}, starting_current, cards=cards)
    provenance: Dict[str, Any] = {"starting_state": bool(recovered), "replayed_events": 0}

    events: List[Tuple[Tuple[str, int], Dict[str, Any], str]] = []
    turns = storage._read_turns(root)
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
        current_patch = state_patch.get("current")
        if isinstance(current_patch, dict):
            events.append((_timestamp(turn.get("saved_at"), index), current_patch, f"turn:{turn.get('turn_number')}"))

    audits = storage._read_json(root / "audits.json", [])
    for index, audit in enumerate(audits if isinstance(audits, list) else []):
        if not isinstance(audit, dict):
            continue
        repairs = audit.get("repairs") if isinstance(audit.get("repairs"), dict) else {}
        state_patch = repairs.get("state_patch") if isinstance(repairs.get("state_patch"), dict) else {}
        current_patch = state_patch.get("current")
        if isinstance(current_patch, dict):
            events.append((_timestamp(audit.get("saved_at"), 100000 + index), current_patch, f"audit:{audit.get('end_turn')}"))

    for _, patch, source_name in sorted(events, key=lambda item: item[0]):
        before = recovered
        recovered = _merge_current(recovered, patch, cards=cards)
        if recovered != before:
            provenance["replayed_events"] += 1
            provenance["last_state_source"] = source_name

    existing_state = storage._read_json(root / "state.json", {})
    existing_current = existing_state.get("current") if isinstance(existing_state.get("current"), dict) else {}
    recovered = _merge_current(recovered, existing_current, cards=cards)

    latest_turn = turns[-1] if turns else None
    latest_turn_number = int(latest_turn.get("turn_number", 0) or 0) if isinstance(latest_turn, dict) else 0
    explicit_present: List[str] = []
    footer_present: List[str] = []
    extracted_present: List[str] = []
    persisted_chronology_present: List[str] = []

    if isinstance(latest_turn, dict):
        header = _parse_scene_header(latest_turn.get("scene_output"))
        if header:
            recovered = _merge_current(recovered, header, cards=cards)
            provenance["scene_header_turn"] = latest_turn_number

        latest_extracted = latest_turn.get("extracted") if isinstance(latest_turn.get("extracted"), dict) else {}
        latest_patch = latest_extracted.get("state_patch") if isinstance(latest_extracted.get("state_patch"), dict) else {}
        if isinstance(latest_patch.get("current"), dict):
            explicit_present = _normalise_present(cards, latest_patch["current"].get("present_characters"))
        extracted_present = _event_participants(latest_extracted.get("chronology"), cards=cards)
        footer_present = _footer_present_ids(latest_turn.get("scene_output"), cards=cards)

    chronology = storage._read_json(root / "chronology.json", [])
    if isinstance(chronology, list) and latest_turn_number:
        latest_events = [
            event
            for event in chronology
            if isinstance(event, dict)
            and int(event.get("turn_number") or event.get("turn") or 0) == latest_turn_number
        ]
        persisted_chronology_present = _event_participants(latest_events, cards=cards)

    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    runtime = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    runtime_present = [
        str(cid)
        for cid, info in runtime.items()
        if isinstance(info, dict)
        and (info.get("present") is True or int(info.get("last_seen_turn", -1) or -1) == int(meta.get("turn_number", 0)))
    ]

    # Presence evidence is ordered from strongest end-of-scene evidence to weaker fallbacks.
    # We do not merge all sources because chronology can mention someone who left before scene end.
    presence_sources = (
        (explicit_present, f"turn:{latest_turn_number}:state_patch"),
        (footer_present, f"turn:{latest_turn_number}:relationship_footer"),
        (runtime_present, "runtime_character_presence"),
        (persisted_chronology_present, f"turn:{latest_turn_number}:persisted_chronology"),
        (extracted_present, f"turn:{latest_turn_number}:extracted_chronology"),
        (_normalise_present(cards, recovered.get("present_characters")), "replayed_current_history"),
    )
    for candidate, source_name in presence_sources:
        candidate = _normalise_present(cards, candidate)
        if candidate:
            recovered["present_characters"] = candidate
            provenance["present_source"] = source_name
            break

    pov_id = _pov_id(state, source, cards)
    present = _normalise_present(cards, recovered.get("present_characters"))
    if pov_id and pov_id not in present:
        present.insert(0, pov_id)
        provenance["pov_reinserted"] = True
    if present:
        recovered["present_characters"] = list(dict.fromkeys(present))

    return recovered, provenance


def recover_session_current(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    before_status = current_recovery_status(session_id)
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    if not before_status["required"]:
        return {
            "ok": True,
            "session_id": session_id,
            "changed": False,
            "turn_number": int(meta.get("turn_number", 0)),
            "current": deepcopy(before_status["current"]),
            "instruction": "Current scene pointer is already usable; no repair was performed.",
        }

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    recovered, provenance = _reconstruct_current(root, source, cards)
    recovered_present = _normalise_present(cards, recovered.get("present_characters"))
    pov_id = _pov_id(state, source, cards)
    meaningful_pointer = any(
        _nonempty(value)
        for value in (
            recovered.get("date") or recovered.get("game_date") or recovered.get("calendar_date"),
            recovered.get("time") or recovered.get("game_time"),
            recovered.get("location") or recovered.get("place") or recovered.get("area"),
        )
    )
    if not meaningful_pointer or not recovered_present or (pov_id and pov_id not in recovered_present):
        raise RuntimeError("CURRENT_RECOVERY_NO_EVIDENCE")

    state = deepcopy(state if isinstance(state, dict) else {})
    state["current"] = recovered
    state = storage._refresh_runtime_presence(state, cards, int(meta.get("turn_number", 0)))
    storage._write_json(root / "state.json", state)

    for name in ("turn_packet.json", "audit_packet.json"):
        path = root / name
        if path.exists():
            path.unlink()

    meta["last_current_recovery"] = {
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "turn_number": int(meta.get("turn_number", 0)),
        "reason": before_status["reasons"],
        "provenance": provenance,
    }
    storage._write_json(root / "meta.json", meta)

    return {
        "ok": True,
        "session_id": session_id,
        "changed": True,
        "turn_number": int(meta.get("turn_number", 0)),
        "current": deepcopy(recovered),
        "provenance": provenance,
        "canon_mutated": False,
        "turn_created": False,
        "instruction": (
            "Technical current-scene pointer repaired without creating a gameplay turn and without changing chronology, memory, source or prior committed turns. "
            "Call resumeSession again, then continue normally."
        ),
    }
