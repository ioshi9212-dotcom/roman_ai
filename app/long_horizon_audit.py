from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .scene_compaction_runtime import load_scene_history


MACRO_AUDIT_INTERVAL = 60
MACRO_CHRONOLOGY_VERSION = 1
MACRO_SUMMARY_MAX_CHARS = 1800
_DATE_RE = re.compile(r"\b(?P<date>\d{2}\.\d{2}\.\d{4})\b")
_TIME_RE = re.compile(r"\b(?P<time>\d{1,2}:\d{2})\b")


def _source_is_v5(source: Dict[str, Any]) -> bool:
    try:
        if int(source.get("version", 1) or 1) >= 5:
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(source.get("profile_schema"), dict)


def macro_due(source: Dict[str, Any], end_turn: int) -> bool:
    return _source_is_v5(source) and int(end_turn) > 0 and int(end_turn) % MACRO_AUDIT_INTERVAL == 0


def macro_range(end_turn: int) -> tuple[int, int]:
    end = max(1, int(end_turn))
    return max(1, end - MACRO_AUDIT_INTERVAL + 1), end


def _event_turn(item: Dict[str, Any]) -> int:
    try:
        return int(item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _story_date(item: Dict[str, Any]) -> str:
    value = item.get("story_date") or item.get("date")
    return str(value or "").strip()


def _turn_date(turn: Dict[str, Any]) -> str:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current = patch.get("current") if isinstance(patch.get("current"), dict) else {}
    value = current.get("date") or current.get("game_date") or current.get("calendar_date")
    if value not in (None, ""):
        return str(value).strip()
    scene = str(turn.get("scene_output") or "")
    match = _DATE_RE.search(scene[:1400])
    return match.group("date") if match else ""


def _turn_calendar(turns: List[Dict[str, Any]], start_turn: int, end_turn: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    for turn in turns:
        number = _event_turn(turn)
        if number < start_turn or number > end_turn:
            continue
        date = _turn_date(turn)
        if not date:
            continue
        if current and current.get("date") == date and int(current.get("end_turn", 0)) + 1 == number:
            current["end_turn"] = number
            continue
        current = {"date": date, "start_turn": number, "end_turn": number}
        rows.append(current)
    return rows


def _date_turns(turns: List[Dict[str, Any]], start_turn: int, end_turn: int) -> Dict[str, List[int]]:
    result: Dict[str, List[int]] = {}
    for turn in turns:
        number = _event_turn(turn)
        if number < start_turn or number > end_turn:
            continue
        date = _turn_date(turn)
        if date:
            result.setdefault(date, []).append(number)
    return result


def _scene_rows_for_range(root, start_turn: int, end_turn: int) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for scene in load_scene_history(root):
        if not isinstance(scene, dict):
            continue
        ranges = scene.get("source_ranges")
        if not isinstance(ranges, list):
            ranges = [[scene.get("start_turn"), scene.get("end_turn")]]
        overlaps = False
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            try:
                left, right = int(pair[0]), int(pair[1])
            except (TypeError, ValueError):
                continue
            if right >= start_turn and left <= end_turn:
                overlaps = True
                break
        if not overlaps:
            continue
        result.append({
            "start_turn": scene.get("start_turn"),
            "end_turn": scene.get("end_turn"),
            "summary": scene.get("summary"),
            "status": scene.get("status"),
            "participants": deepcopy(scene.get("participants", [])),
            "locations": deepcopy(scene.get("locations", [])),
        })
    return result


def relationship_audit(
    state: Dict[str, Any],
    turns: List[Dict[str, Any]],
    character_ids: List[str],
    start_turn: int,
    end_turn: int,
) -> Dict[str, Any]:
    wanted = set(str(value) for value in character_ids if value)
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}

    changes: List[Dict[str, Any]] = []
    for turn in turns:
        number = _event_turn(turn)
        if number < start_turn or number > end_turn:
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        updates = extracted.get("relationship_updates")
        if isinstance(updates, list) and updates:
            changes.append({
                "turn_number": number,
                "updates": deepcopy(updates),
            })
            for row in updates:
                if isinstance(row, dict) and row.get("character_id"):
                    wanted.add(str(row["character_id"]))

    return {
        "current_numeric": {
            cid: deepcopy(flat[cid])
            for cid in wanted
            if isinstance(flat.get(cid), dict)
        },
        "current_documents": {
            cid: deepcopy(docs[cid])
            for cid in wanted
            if isinstance(docs.get(cid), dict)
        },
        "changes_in_audit_range": changes,
        "contract": {
            "direction": "NPC -> POV",
            "check_no_dimension_loss_or_silent_rename": True,
            "check_numeric_deltas_against_saved_baseline": True,
            "check_current_dynamic_beliefs_and_unresolved_are_causal": True,
            "repair_path": "repairs.state_patch.relationships / relationship_documents only when audited evidence proves drift",
        },
    }


def cast_audit(
    state: Dict[str, Any],
    character_ids: List[str],
    start_turn: int,
    end_turn: int,
    *,
    include_all: bool = False,
) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    registry = world.get("cast_registry") if isinstance(world.get("cast_registry"), dict) else {}
    wanted = set(str(value) for value in character_ids if value)

    rows: List[Dict[str, Any]] = []
    for cid, raw in registry.items():
        if not isinstance(raw, dict):
            continue
        appearance = int(raw.get("last_appearance_turn", 0) or 0)
        contact = int(raw.get("last_contact_turn", 0) or 0)
        touched = start_turn <= max(appearance, contact) <= end_turn
        if not include_all and str(cid) not in wanted and not touched:
            continue
        rows.append({
            key: deepcopy(raw.get(key))
            for key in (
                "character_id", "name", "importance", "story_function", "status",
                "last_appearance_turn", "last_appearance_game_day",
                "last_contact_turn", "last_contact_game_day", "last_contact_mode",
                "last_appearance_location", "appearance_count", "last_meaningful_turn", "last_meaningful_event",
            )
            if raw.get(key) not in (None, "", [], {})
        })

    return {
        "registry_rows": rows,
        "contract": {
            "physical_appearance": "updates last_appearance_turn/day and contact",
            "remote_call_or_message": "updates last_contact_turn/day only",
            "check_against_audited_turns": True,
            "repair_path": "repairs.state_patch.world.cast_registry only when evidence proves stale metadata",
        },
    }


def build_macro_payload(
    root,
    *,
    source: Dict[str, Any],
    state: Dict[str, Any],
    chronology: Any,
    turns: List[Dict[str, Any]],
    end_turn: int,
) -> Dict[str, Any] | None:
    if not macro_due(source, end_turn):
        return None

    start_turn, end_turn = macro_range(end_turn)
    chronology_rows = chronology if isinstance(chronology, list) else []
    events = [
        deepcopy(event)
        for event in chronology_rows
        if isinstance(event, dict) and start_turn <= _event_turn(event) <= end_turn
    ]
    compact_events = []
    for event in events:
        row = {
            "turn_number": _event_turn(event),
            "date": _story_date(event),
            "event": event.get("event") or event.get("summary") or event.get("text") or event.get("description"),
            "importance": event.get("importance"),
            "time_critical": event.get("time_critical") is True,
            "exact_time": event.get("exact_time"),
            "participants": deepcopy(
                event.get("participants_present")
                or event.get("participants")
                or event.get("character_ids")
                or []
            ),
        }
        compact_events.append({k: v for k, v in row.items() if v not in (None, "", [], {})})

    relationship_changes = []
    for turn in turns:
        number = _event_turn(turn)
        if number < start_turn or number > end_turn:
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        rows = extracted.get("relationship_updates")
        if isinstance(rows, list) and rows:
            relationship_changes.append({"turn_number": number, "updates": deepcopy(rows)})

    return {
        "required": True,
        "macro_interval": MACRO_AUDIT_INTERVAL,
        "macro_range": [start_turn, end_turn],
        "turn_calendar": _turn_calendar(turns, start_turn, end_turn),
        "prior_scene_summaries": _scene_rows_for_range(root, start_turn, end_turn),
        "chronology_events_before_compaction": compact_events,
        "relationship_changes_60_turns": relationship_changes,
        "cast_registry_audit": cast_audit(
            state,
            [],
            start_turn,
            end_turn,
            include_all=True,
        ),
        "output_required": {
            "repairs.chronology_compactions": (
                "List of important date-based paragraphs only. Each row: date + summary; optional participants/importance. "
                "exact_time only when the exact time itself matters causally. Routine eating, showering, smoking, toilet, "
                "ordinary travel and repeated atmosphere are omitted unless they caused a durable consequence."
            ),
        },
        "contract": {
            "raw_turns_remain_immutable_evidence": True,
            "chronology_json_is_working_long_term_history": True,
            "replace_raw_chronology_in_macro_range": True,
            "keep_only_distinct_plot_relationship_revelation_conflict_decision_and_consequence_events": True,
            "group_by_story_date": True,
            "do_not_repeat_same_fact_across_paragraphs": True,
            "do_not_write_clock_time_unless_time_critical": True,
        },
        "instruction": (
            "60-TURN MACRO AUDIT. После обычной проверки последних 15 ходов собери repairs.chronology_compactions "
            "по macro_range. Это не дополнительный пересказ поверх старой chronology: commit заменит сырые chronology-события "
            "этого диапазона этими короткими датированными абзацами. Удали бытовую воду и повторы, сохрани только важное."
        ),
    }


def _apply_macro_chronology_compaction_core(
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    chronology: Any,
    repairs: Dict[str, Any],
    *,
    end_turn: int,
) -> List[Dict[str, Any]]:
    values = [deepcopy(row) for row in chronology if isinstance(row, dict)] if isinstance(chronology, list) else []
    if not macro_due(source, end_turn):
        return values

    raw_rows = repairs.get("chronology_compactions")
    if not isinstance(raw_rows, list):
        raise RuntimeError("MACRO_CHRONOLOGY_COMPACTION_REQUIRED")

    start_turn, end_turn = macro_range(end_turn)
    turns = [
        row for row in turns
        if start_turn <= _event_turn(row) <= end_turn
    ]
    turns_by_date = _date_turns(turns, start_turn, end_turn)

    source_events = [
        row for row in values
        if start_turn <= _event_turn(row) <= end_turn
    ]
    important_dates = {
        _story_date(row)
        for row in source_events
        if str(row.get("importance") or "").casefold() in {"major", "anchor", "critical"}
        or row.get("anchor") is True
    }
    important_dates.discard("")

    normalized: List[Dict[str, Any]] = []
    represented_dates: set[str] = set()
    for index, raw in enumerate(raw_rows, 1):
        if not isinstance(raw, dict):
            raise RuntimeError("MACRO_CHRONOLOGY_COMPACTION_INVALID")
        date = str(raw.get("date") or raw.get("story_date") or "").strip()
        summary = " ".join(str(raw.get("summary") or raw.get("event") or "").split()).strip()
        if not date or not _DATE_RE.fullmatch(date) or len(summary) < 20 or len(summary) > MACRO_SUMMARY_MAX_CHARS:
            raise RuntimeError("MACRO_CHRONOLOGY_COMPACTION_INVALID")
        represented_dates.add(date)

        date_turn_values = turns_by_date.get(date, [])
        source_range = [
            min(date_turn_values) if date_turn_values else start_turn,
            max(date_turn_values) if date_turn_values else end_turn,
        ]
        importance = str(raw.get("importance") or "major").casefold().strip()
        if importance not in {"normal", "major", "anchor", "critical"}:
            importance = "major"

        participants = raw.get("participants")
        if isinstance(participants, str):
            participants = [participants]
        if not isinstance(participants, list):
            participants = []

        critical_times: List[str] = []
        for event in source_events:
            if _story_date(event) != date or event.get("time_critical") is not True:
                continue
            exact = str(event.get("exact_time") or "").strip()
            if exact and exact not in critical_times:
                critical_times.append(exact)

        item: Dict[str, Any] = {
            "event_id": f"macro_{end_turn}_{index}",
            "turn_number": source_range[1],
            "story_date": date,
            "event": summary,
            "importance": importance,
            "participants_present": [str(value) for value in participants if value],
            "canonical_macro_compaction": True,
            "source_turn_range": source_range,
            "compacted_at_turn": int(end_turn),
        }
        exact_time = str(raw.get("exact_time") or "").strip()
        if exact_time:
            item["exact_time"] = exact_time
            item["time_critical"] = True
        elif critical_times:
            item["critical_times"] = critical_times
            item["time_critical"] = True
        normalized.append({k: v for k, v in item.items() if v not in (None, "", [], {})})

    if important_dates - represented_dates:
        raise RuntimeError("MACRO_CHRONOLOGY_IMPORTANT_DATE_MISSING")

    kept = [
        row for row in values
        if not (start_turn <= _event_turn(row) <= end_turn)
    ]
    return sorted(
        [*kept, *normalized],
        key=lambda row: (_event_turn(row), str(row.get("event_id") or "")),
    )


def apply_macro_chronology_compaction(
    root,
    chronology: Any,
    repairs: Dict[str, Any],
    *,
    end_turn: int,
) -> List[Dict[str, Any]]:
    source = storage._read_json(root / "source.json", {})
    turns = storage._read_turns(root)
    return _apply_macro_chronology_compaction_core(
        source,
        turns,
        chronology,
        repairs,
        end_turn=end_turn,
    )


def replay_macro_chronology_compaction(
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    chronology: Any,
    repairs: Dict[str, Any],
    *,
    end_turn: int,
) -> List[Dict[str, Any]]:
    return _apply_macro_chronology_compaction_core(
        source,
        turns,
        chronology,
        repairs,
        end_turn=end_turn,
    )
