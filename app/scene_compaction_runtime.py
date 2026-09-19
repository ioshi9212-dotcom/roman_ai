from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

from . import storage


SCENE_MEMORY_FILE = "scene_memory.json"
SCENE_MEMORY_VERSION = 1
SCENE_SUMMARY_MIN_CHARS = 60
SCENE_SUMMARY_MAX_CHARS = 1200
MEMORY_SUMMARY_MAX_CHARS = 900
_MEMORY_ID_KEYS = {
    "knowledge": "fact_id",
    "experiences": "event_id",
    "dialogue_memory": "topic_id",
}
_IMPORTANCE_RANK = {"normal": 0, "major": 1, "anchor": 2, "critical": 3}
_DURABILITY_FLAGS = ("anchor", "durable", "pinned", "permanent")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _record_turn(item: Dict[str, Any]) -> int:
    for key in ("learned_turn", "turn_number", "turn", "source_turn"):
        try:
            if item.get(key) not in (None, ""):
                return int(item[key])
        except (TypeError, ValueError):
            pass
    return 0


def _record_id(item: Dict[str, Any], memory_type: str) -> str:
    key = _MEMORY_ID_KEYS[memory_type]
    return str(item.get(key) or "")


def _load_store(root) -> Dict[str, Any]:
    raw = storage._read_json(root / SCENE_MEMORY_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        scenes = []
    return {
        "version": SCENE_MEMORY_VERSION,
        "scenes": [deepcopy(row) for row in scenes if isinstance(row, dict)],
    }


def load_scene_history(root) -> List[Dict[str, Any]]:
    return _load_store(root)["scenes"]


def _next_scene_id(store: Dict[str, Any]) -> str:
    highest = 0
    for row in store.get("scenes", []):
        raw = str(row.get("scene_id") or "")
        if raw.startswith("scene_"):
            try:
                highest = max(highest, int(raw.split("_", 1)[1]))
            except (TypeError, ValueError):
                pass
    return f"scene_{highest + 1:06d}"


def _ranges(row: Dict[str, Any]) -> List[List[int]]:
    values = row.get("source_ranges")
    result: List[List[int]] = []
    if isinstance(values, list):
        for pair in values:
            if isinstance(pair, list) and len(pair) == 2:
                try:
                    start, end = int(pair[0]), int(pair[1])
                except (TypeError, ValueError):
                    continue
                if start > 0 and end >= start:
                    result.append([start, end])
    if not result:
        try:
            start, end = int(row.get("start_turn") or 0), int(row.get("end_turn") or 0)
        except (TypeError, ValueError):
            start, end = 0, 0
        if start > 0 and end >= start:
            result.append([start, end])
    return result


def covered_turns(root) -> set[int]:
    result: set[int] = set()
    for row in load_scene_history(root):
        for start, end in _ranges(row):
            result.update(range(start, end + 1))
    return result


def _open_scene(store: Dict[str, Any]) -> Dict[str, Any] | None:
    for row in reversed(store.get("scenes", [])):
        if str(row.get("status") or "").casefold() == "open":
            return row
    return None


def audit_scene_context(root) -> Dict[str, Any]:
    scenes = load_scene_history(root)
    open_scene = next(
        (deepcopy(row) for row in reversed(scenes) if str(row.get("status") or "").casefold() == "open"),
        None,
    )
    return {
        "open_scene_before_range": open_scene,
        "recent_scene_summaries": [deepcopy(row) for row in scenes[-12:]],
        "contract": {
            "raw_turns_are_immutable_evidence": True,
            "one_dense_sentence_per_scene": True,
            "all_audit_turns_must_be_covered_exactly_once": True,
            "same_scene_across_audits_reuses_scene_id": True,
            "summary_rule": (
                "Compress the scene into ONE dense factual sentence that preserves its arc: who initiated what, "
                "how it developed, important dialogue/revelations/choices, and how the scene ended or where it paused. "
                "Do not replace a detailed scene with a vague label such as 'they got closer' or 'she chose both'."
            ),
        },
    }


def _normalise_scene_rows(
    rows: Any,
    *,
    start_turn: int,
    end_turn: int,
    store: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("SCENE_COMPACTION_REQUIRED")

    existing = {
        str(row.get("scene_id")): row
        for row in store.get("scenes", [])
        if isinstance(row, dict) and row.get("scene_id")
    }
    next_expected = int(start_turn)
    result: List[Dict[str, Any]] = []
    used_existing: set[str] = set()

    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("SCENE_COMPACTION_INVALID")
        try:
            row_start = int(raw.get("start_turn"))
            row_end = int(raw.get("end_turn"))
        except (TypeError, ValueError):
            raise RuntimeError("SCENE_COMPACTION_INVALID")
        if row_start != next_expected or row_end < row_start or row_end > int(end_turn):
            raise RuntimeError("SCENE_COMPACTION_COVERAGE_INVALID")

        summary = _clean_text(raw.get("summary"))
        if len(summary) < SCENE_SUMMARY_MIN_CHARS or len(summary) > SCENE_SUMMARY_MAX_CHARS:
            raise RuntimeError("SCENE_COMPACTION_SUMMARY_INVALID")

        status = str(raw.get("status") or ("open" if row_end == int(end_turn) else "closed")).casefold().strip()
        if status not in {"open", "closed"}:
            raise RuntimeError("SCENE_COMPACTION_INVALID")
        if status == "open" and row_end != int(end_turn):
            raise RuntimeError("SCENE_COMPACTION_INVALID")

        scene_id = str(raw.get("scene_id") or "").strip()
        if scene_id:
            prior = existing.get(scene_id)
            if not prior or str(prior.get("status") or "").casefold() != "open" or scene_id in used_existing:
                raise RuntimeError("SCENE_COMPACTION_SCENE_ID_INVALID")
            used_existing.add(scene_id)
        else:
            scene_id = _next_scene_id({"scenes": [*store.get("scenes", []), *result]})

        participants = raw.get("participants")
        participants = [str(value) for value in participants if value] if isinstance(participants, list) else []
        location = _clean_text(raw.get("location"))

        result.append(
            {
                "scene_id": scene_id,
                "start_turn": row_start,
                "end_turn": row_end,
                "summary": summary,
                "status": status,
                "participants": list(dict.fromkeys(participants)),
                "location": location,
            }
        )
        next_expected = row_end + 1

    if next_expected != int(end_turn) + 1:
        raise RuntimeError("SCENE_COMPACTION_COVERAGE_INVALID")
    return result


def _merge_scene_rows(
    store: Dict[str, Any],
    rows: List[Dict[str, Any]],
    *,
    audit_end_turn: int,
) -> Dict[str, Any]:
    result = deepcopy(store)
    scenes = result.setdefault("scenes", [])
    by_id = {str(row.get("scene_id")): row for row in scenes if isinstance(row, dict) and row.get("scene_id")}

    current_open = _open_scene(result)
    if current_open and rows and str(rows[0].get("scene_id")) != str(current_open.get("scene_id")):
        current_open["status"] = "closed"
        current_open["closed_at_audit_turn"] = int(audit_end_turn)

    for row in rows:
        scene_id = str(row["scene_id"])
        prior = by_id.get(scene_id)
        if prior is None:
            prior = {
                "scene_id": scene_id,
                "start_turn": int(row["start_turn"]),
                "end_turn": int(row["end_turn"]),
                "summary": row["summary"],
                "status": row["status"],
                "participants": deepcopy(row["participants"]),
                "locations": [row["location"]] if row.get("location") else [],
                "source_ranges": [[int(row["start_turn"]), int(row["end_turn"])]],
                "created_at_audit_turn": int(audit_end_turn),
                "last_compacted_at_audit_turn": int(audit_end_turn),
            }
            scenes.append(prior)
            by_id[scene_id] = prior
            continue

        prior["start_turn"] = min(int(prior.get("start_turn") or row["start_turn"]), int(row["start_turn"]))
        prior["end_turn"] = max(int(prior.get("end_turn") or row["end_turn"]), int(row["end_turn"]))
        prior["summary"] = row["summary"]
        prior["status"] = row["status"]
        prior["participants"] = list(
            dict.fromkeys(
                [
                    *([str(v) for v in prior.get("participants", []) if v] if isinstance(prior.get("participants"), list) else []),
                    *row["participants"],
                ]
            )
        )
        locations = [str(v) for v in prior.get("locations", []) if v] if isinstance(prior.get("locations"), list) else []
        if row.get("location"):
            locations.append(str(row["location"]))
        prior["locations"] = list(dict.fromkeys(locations))
        ranges = _ranges(prior)
        ranges.append([int(row["start_turn"]), int(row["end_turn"])])
        prior["source_ranges"] = ranges
        prior["last_compacted_at_audit_turn"] = int(audit_end_turn)

    return result


def _scene_for_turn(rows: List[Dict[str, Any]], turn: int) -> str | None:
    for row in rows:
        if int(row["start_turn"]) <= int(turn) <= int(row["end_turn"]):
            return str(row["scene_id"])
    return None


def _compact_chronology(
    chronology: Any,
    rows: List[Dict[str, Any]],
    *,
    audit_end_turn: int,
) -> List[Dict[str, Any]]:
    values = [deepcopy(item) for item in chronology if isinstance(item, dict)] if isinstance(chronology, list) else []
    for item in values:
        turn = _record_turn(item)
        scene_id = _scene_for_turn(rows, turn)
        if scene_id:
            item["compacted_scene_id"] = scene_id
            item["compacted_at_audit_turn"] = int(audit_end_turn)
    return values


def _memory_source_lookup(bucket: Dict[str, Any], memory_type: str) -> Dict[str, Dict[str, Any]]:
    values = bucket.get(memory_type)
    rows = values if isinstance(values, list) else []
    return {
        _record_id(item, memory_type): item
        for item in rows
        if isinstance(item, dict) and _record_id(item, memory_type)
    }


def _source_confidence_metadata(
    source_rows: List[Dict[str, Any]],
    source_ids: List[str],
) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    for item, source_id in zip(source_rows, source_ids):
        inherited = item.get("source_confidences")
        if item.get("canonical_compaction") is True and isinstance(inherited, list) and inherited:
            entries.extend(
                deepcopy(row)
                for row in inherited
                if isinstance(row, dict) and row.get("source_id")
            )
        elif "confidence" in item:
            entries.append({"source_id": source_id, "confidence": deepcopy(item.get("confidence"))})

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in entries:
        source_id = str(row.get("source_id") or "")
        if source_id and source_id not in seen:
            seen.add(source_id)
            deduped.append(row)
    if not deduped:
        return {}

    values: List[Any] = []
    for row in deduped:
        value = row.get("confidence")
        if not any(value == existing for existing in values):
            values.append(deepcopy(value))

    result: Dict[str, Any] = {"source_confidences": deduped}
    if len(values) == 1:
        result["confidence"] = deepcopy(values[0])
        return result

    non_certain = [value for value in values if str(value).casefold().strip() != "certain"]
    result["confidence"] = deepcopy(non_certain[0]) if len(non_certain) == 1 else "mixed"
    return result


def _durability_metadata(source_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    importances: List[str] = []
    for item in source_rows:
        inherited = item.get("source_importance")
        if item.get("canonical_compaction") is True and isinstance(inherited, list):
            importances.extend(
                str(value).casefold().strip()
                for value in inherited
                if str(value or "").strip()
            )
        own = str(item.get("importance") or "").casefold().strip()
        if own:
            importances.append(own)

    distinct = list(dict.fromkeys(importances))
    result: Dict[str, Any] = {}
    if distinct:
        result["importance"] = max(
            distinct,
            key=lambda value: (_IMPORTANCE_RANK.get(value, 0), -distinct.index(value)),
        )
        result["source_importance"] = distinct

    for key in _DURABILITY_FLAGS:
        if any(item.get(key) is True for item in source_rows):
            result[key] = True
    return result


def _canonical_memory_record(
    *,
    character_id: str,
    memory_type: str,
    source_rows: List[Dict[str, Any]],
    source_ids: List[str],
    summary: str,
    audit_end_turn: int,
) -> Dict[str, Any]:
    turns: set[int] = set()
    provenance: List[str] = []
    for item, source_id in zip(source_rows, source_ids):
        raw_turns = item.get("source_turns")
        if isinstance(raw_turns, list):
            for raw_turn in raw_turns:
                try:
                    turn = int(raw_turn)
                except (TypeError, ValueError):
                    continue
                if turn > 0:
                    turns.add(turn)
        turn = _record_turn(item)
        if turn > 0:
            turns.add(turn)

        merged = item.get("merged_from")
        if item.get("canonical_compaction") is True and isinstance(merged, list) and merged:
            provenance.extend(str(value) for value in merged if value)
        else:
            provenance.append(source_id)

    ordered_turns = sorted(turns)
    provenance = list(dict.fromkeys(provenance))
    digest = hashlib.sha256(
        f"{character_id}|{memory_type}|{'|'.join(provenance)}|{summary}".encode("utf-8")
    ).hexdigest()[:12]
    canonical_id = f"cmp_{memory_type}_{digest}"
    common = {
        "character_id": character_id,
        "source_turns": ordered_turns,
        "merged_from": provenance,
        "canonical_compaction": True,
        "compacted_at_audit_turn": int(audit_end_turn),
    }

    if memory_type == "knowledge":
        first_turn = min(ordered_turns) if ordered_turns else int(audit_end_turn)
        last_turn = max(ordered_turns) if ordered_turns else int(audit_end_turn)
        return {
            **common,
            **_source_confidence_metadata(source_rows, source_ids),
            **_durability_metadata(source_rows),
            "fact_id": canonical_id,
            "fact": summary,
            "learned_turn": first_turn,
            "first_learned_turn": first_turn,
            "last_learned_turn": last_turn,
        }
    if memory_type == "experiences":
        return {
            **common,
            "event_id": canonical_id,
            "summary": summary,
            "turn": max(ordered_turns) if ordered_turns else int(audit_end_turn),
        }

    participants: List[str] = []
    for item in source_rows:
        raw = item.get("participants")
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            participants.extend(str(value) for value in raw if value)
    return {
        **common,
        "topic_id": canonical_id,
        "summary": summary,
        "turn": max(ordered_turns) if ordered_turns else int(audit_end_turn),
        "participants": list(dict.fromkeys(participants)),
    }


def _apply_memory_compactions(
    memory: Dict[str, Any],
    rows: Any,
    *,
    start_turn: int,
    end_turn: int,
) -> Dict[str, Any]:
    if rows in (None, []):
        return deepcopy(memory)
    if not isinstance(rows, list):
        raise RuntimeError("MEMORY_COMPACTION_INVALID")

    result = storage._normalise_memory(deepcopy(memory))
    characters = result.get("characters") if isinstance(result.get("characters"), dict) else {}
    used: set[Tuple[str, str, str]] = set()

    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("MEMORY_COMPACTION_INVALID")
        character_id = str(raw.get("character_id") or "").strip()
        memory_type = str(raw.get("memory_type") or "").strip()
        if memory_type not in _MEMORY_ID_KEYS or character_id not in characters:
            raise RuntimeError("MEMORY_COMPACTION_INVALID")

        source_ids = raw.get("source_ids")
        source_ids = [str(value) for value in source_ids if value] if isinstance(source_ids, list) else []
        source_ids = list(dict.fromkeys(source_ids))
        summary = _clean_text(raw.get("summary"))
        if not source_ids or not summary or len(summary) > MEMORY_SUMMARY_MAX_CHARS:
            raise RuntimeError("MEMORY_COMPACTION_INVALID")

        bucket = characters.get(character_id)
        bucket = bucket if isinstance(bucket, dict) else {}
        lookup = _memory_source_lookup(bucket, memory_type)
        source_rows: List[Dict[str, Any]] = []
        has_current_audit_evidence = False
        for source_id in source_ids:
            key = (character_id, memory_type, source_id)
            if key in used:
                raise RuntimeError("MEMORY_COMPACTION_SOURCE_REUSED")
            item = lookup.get(source_id)
            if item is None or item.get("superseded_by"):
                raise RuntimeError("MEMORY_COMPACTION_SOURCE_UNKNOWN")

            record_turns = set()
            raw_source_turns = item.get("source_turns")
            if isinstance(raw_source_turns, list):
                for raw_turn in raw_source_turns:
                    try:
                        record_turns.add(int(raw_turn))
                    except (TypeError, ValueError):
                        pass
            turn = _record_turn(item)
            if turn > 0:
                record_turns.add(turn)

            current = any(int(start_turn) <= value <= int(end_turn) for value in record_turns)
            if current:
                has_current_audit_evidence = True
            elif item.get("canonical_compaction") is not True:
                # Old raw records are immutable evidence, not arbitrary rewrite targets.
                # Cross-audit merging may roll a prior canonical record forward, but only
                # together with fresh records from the current audit.
                raise RuntimeError("MEMORY_COMPACTION_SOURCE_OUT_OF_RANGE")

            used.add(key)
            source_rows.append(item)

        if not has_current_audit_evidence:
            raise RuntimeError("MEMORY_COMPACTION_SOURCE_OUT_OF_RANGE")

        canonical = _canonical_memory_record(
            character_id=character_id,
            memory_type=memory_type,
            source_rows=source_rows,
            source_ids=source_ids,
            summary=summary,
            audit_end_turn=end_turn,
        )
        id_key = _MEMORY_ID_KEYS[memory_type]
        canonical_id = str(canonical[id_key])
        for item in source_rows:
            item["superseded_by"] = canonical_id
            item["compacted_at_audit_turn"] = int(end_turn)
            item["raw_evidence_preserved"] = True
        storage._upsert_by_id(bucket.setdefault(memory_type, []), canonical, id_key)

    return result


def active_memory_records(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [
        deepcopy(item)
        for item in values
        if isinstance(item, dict) and not item.get("superseded_by")
    ]


def apply_audit_compactions(
    root,
    repairs: Dict[str, Any],
    *,
    start_turn: int,
    end_turn: int,
    memory: Dict[str, Any],
    chronology: Any,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    store = _load_store(root)
    scene_rows = _normalise_scene_rows(
        repairs.get("scene_compactions"),
        start_turn=start_turn,
        end_turn=end_turn,
        store=store,
    )
    store = _merge_scene_rows(store, scene_rows, audit_end_turn=end_turn)
    compacted_memory = _apply_memory_compactions(
        memory,
        repairs.get("memory_compactions"),
        start_turn=start_turn,
        end_turn=end_turn,
    )
    compacted_chronology = _compact_chronology(
        chronology,
        scene_rows,
        audit_end_turn=end_turn,
    )
    return compacted_memory, compacted_chronology, store, scene_rows


def replay_audit_compactions(
    memory: Dict[str, Any],
    chronology: Any,
    repairs: Dict[str, Any],
    *,
    start_turn: int,
    end_turn: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    scene_rows = repairs.get("scene_compactions")
    scene_rows = [deepcopy(row) for row in scene_rows if isinstance(row, dict)] if isinstance(scene_rows, list) else []
    replayed_memory = _apply_memory_compactions(
        memory,
        repairs.get("memory_compactions"),
        start_turn=start_turn,
        end_turn=end_turn,
    )
    replayed_chronology = _compact_chronology(
        chronology,
        scene_rows,
        audit_end_turn=end_turn,
    )
    return replayed_memory, replayed_chronology


def scene_store_from_audits(audits: Any) -> Dict[str, Any]:
    store: Dict[str, Any] = {"version": SCENE_MEMORY_VERSION, "scenes": []}
    values = [row for row in audits if isinstance(row, dict)] if isinstance(audits, list) else []
    values.sort(key=lambda row: int(row.get("end_turn", 0) or 0))
    for audit in values:
        repairs = audit.get("repairs") if isinstance(audit.get("repairs"), dict) else {}
        rows = repairs.get("scene_compactions")
        rows = [deepcopy(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if not rows:
            continue
        store = _merge_scene_rows(
            store,
            rows,
            audit_end_turn=int(audit.get("end_turn", 0) or 0),
        )
    return store
