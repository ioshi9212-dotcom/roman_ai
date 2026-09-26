from __future__ import annotations

import json
import math
import secrets
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .scene_compaction_runtime import load_scene_history
from .transactional_storage import json_text, write_batch

BLOCK_SIZE = 100
RECENT_TURN_COUNT = 15
RECENT_SCENE_COUNT = 8
MIGRATION_VERSION = 2


def _migration_path(session_id: str):
    root = storage.DATA_DIR / "continuation_migrations"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.json"


def _record_turn(item: Dict[str, Any]) -> int:
    for key in ("learned_turn", "turn_number", "turn", "source_turn"):
        try:
            if item.get(key) not in (None, ""):
                return int(item[key])
        except (TypeError, ValueError):
            pass
    return 0


def _load_source_parts(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    source = storage._read_json(root / "source.json", {})
    return {
        "root": root,
        "source": source,
        "cards": storage._load_cards(root, source),
        "state": storage._read_json(root / "state.json", {}),
        "memory": storage._normalise_memory(storage._read_json(root / "memory.json", {})),
        "chronology": storage._read_json(root / "chronology.json", []),
        "meta": storage._read_json(root / "meta.json", {}),
        "turns": storage._read_turns(root),
        "scenes": load_scene_history(root),
    }


def _memory_counts(memory: Dict[str, Any]) -> Dict[str, int]:
    totals = {"characters": 0, "knowledge_journal": 0, "knowledge": 0, "experiences": 0, "dialogue_memory": 0}
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    totals["characters"] = len(characters)
    for bucket in characters.values():
        if not isinstance(bucket, dict):
            continue
        for key in ("knowledge_journal", "knowledge", "experiences", "dialogue_memory"):
            values = bucket.get(key)
            totals[key] += len(values) if isinstance(values, list) else 0
    return totals


def build_continuation_preview(session_id: str) -> Dict[str, Any]:
    p = _load_source_parts(session_id)
    current_turn = int(p["meta"].get("turn_number") or len(p["turns"]) or 0)
    return {
        "ok": True,
        "read_only": True,
        "source_session_id": session_id,
        "source_turn": current_turn,
        "counts": {
            "characters": len(p["cards"]),
            "source_turns": len(p["turns"]),
            "chronology": len(p["chronology"]) if isinstance(p["chronology"], list) else 0,
            "memory": _memory_counts(p["memory"]),
            "recent_turns_bridge": min(RECENT_TURN_COUNT, len(p["turns"])),
            "recent_scenes_reference": min(RECENT_SCENE_COUNT, len(p["scenes"])),
        },
        "current": deepcopy(p["state"].get("current", {})),
        "pov": deepcopy(p["state"].get("pov", {})),
        "open_threads": deepcopy(p["state"].get("threads", {})),
        "requires_semantic_compaction": True,
        "instruction": "Run prepareContinuationCompaction. Do not create a continuation session directly from this preview.",
    }


def _load_migration(session_id: str) -> Dict[str, Any]:
    value = storage._read_json(_migration_path(session_id), {})
    if not isinstance(value, dict) or not value:
        raise RuntimeError("CONTINUATION_COMPACTION_REQUIRED")
    return value


def _save_migration(session_id: str, value: Dict[str, Any]) -> None:
    storage._write_json(_migration_path(session_id), value)


def prepare_continuation_compaction(session_id: str) -> Dict[str, Any]:
    p = _load_source_parts(session_id)
    source_turn = int(p["meta"].get("turn_number") or len(p["turns"]) or 0)
    path = _migration_path(session_id)
    existing = storage._read_json(path, {})
    if (
        isinstance(existing, dict)
        and existing.get("version") == MIGRATION_VERSION
        and int(existing.get("source_turn", -1)) == source_turn
        and existing.get("migration_id")
    ):
        migration = existing
    else:
        migration = {
            "version": MIGRATION_VERSION,
            "migration_id": secrets.token_urlsafe(12),
            "source_session_id": session_id,
            "source_turn": source_turn,
            "block_size": BLOCK_SIZE,
            "block_count": max(1, math.ceil(max(1, source_turn) / BLOCK_SIZE)),
            "block_summaries": {},
            "final_package": None,
            "active_read": None,
        }
        _save_migration(session_id, migration)
    done = {int(k) for k in migration.get("block_summaries", {}).keys()}
    next_block = next((i for i in range(int(migration["block_count"])) if i not in done), None)
    return {
        "ok": True,
        "migration_id": migration["migration_id"],
        "source_turn": source_turn,
        "block_size": BLOCK_SIZE,
        "block_count": migration["block_count"],
        "completed_blocks": sorted(done),
        "next_block_index": next_block,
        "ready_for_finalization": next_block is None,
        "instruction": "Process every block in order. For each block call prepareContinuationBlockRead, read all chunks, then commitContinuationBlock.",
    }


def _validate_migration(migration: Dict[str, Any], migration_id: str) -> None:
    if str(migration.get("migration_id") or "") != str(migration_id):
        raise PermissionError("INVALID_CONTINUATION_MIGRATION")


def _block_range(migration: Dict[str, Any], block_index: int) -> tuple[int, int]:
    count = int(migration.get("block_count", 0))
    if block_index < 0 or block_index >= count:
        raise IndexError("CONTINUATION_BLOCK_OUT_OF_RANGE")
    start = block_index * int(migration.get("block_size", BLOCK_SIZE)) + 1
    end = min(int(migration["source_turn"]), start + int(migration.get("block_size", BLOCK_SIZE)) - 1)
    return start, end


def _memory_for_range(memory: Dict[str, Any], start: int, end: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    for cid, raw in characters.items():
        if not isinstance(raw, dict):
            continue
        bucket: Dict[str, Any] = {}
        for key in ("knowledge_journal", "knowledge", "experiences", "dialogue_memory"):
            values = raw.get(key) if isinstance(raw.get(key), list) else []
            rows = [
                deepcopy(x) for x in values
                if isinstance(x, dict)
                and (
                    start <= _record_turn(x) <= end
                    or (start == 1 and _record_turn(x) == 0)
                )
            ]
            if rows:
                bucket[key] = rows
        if bucket:
            result[str(cid)] = bucket
    return result


def prepare_continuation_block_read(session_id: str, migration_id: str, block_index: int) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    _validate_migration(migration, migration_id)
    start, end = _block_range(migration, block_index)
    p = _load_source_parts(session_id)
    chronology = [
        deepcopy(x) for x in p["chronology"]
        if isinstance(x, dict)
        and (
            start <= _record_turn(x) <= end
            or (start == 1 and _record_turn(x) == 0)
        )
    ] if isinstance(p["chronology"], list) else []
    scene_summaries = []
    for scene in p["scenes"]:
        if not isinstance(scene, dict):
            continue
        try:
            scene_start = int(scene.get("start_turn") or 0)
            scene_end = int(scene.get("end_turn") or 0)
        except (TypeError, ValueError):
            continue
        if scene_end < start or scene_start > end:
            continue
        scene_summaries.append(deepcopy(scene))

    turn_evidence = []
    for turn in p["turns"]:
        try:
            turn_number = int(turn.get("turn_number", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not start <= turn_number <= end:
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        evidence = {"turn_number": turn_number}
        user_input = str(turn.get("user_input") or "").strip()
        if user_input:
            evidence["user_input"] = user_input[:1200]
        for key in ("chronology", "relationship_updates", "story_thread_updates", "character_upserts"):
            value = extracted.get(key)
            if isinstance(value, list) and value:
                evidence[key] = deepcopy(value)
        state_patch = extracted.get("state_patch")
        if isinstance(state_patch, dict) and isinstance(state_patch.get("current"), dict) and state_patch["current"]:
            evidence["current_patch"] = deepcopy(state_patch["current"])
        if len(evidence) > 1:
            turn_evidence.append(evidence)

    payload = {
        "migration_id": migration_id,
        "block_index": block_index,
        "turn_range": [start, end],
        "character_registry": [
            {"character_id": storage._card_id(c), "name": storage._card_name(c), "role": storage._card_role(c)}
            for c in p["cards"] if storage._card_id(c)
        ],
        "scene_summaries": scene_summaries,
        "turn_evidence": turn_evidence,
        "chronology_records": chronology,
        "personal_memory_records": _memory_for_range(p["memory"], start, end),
        "output_contract": {
            "chronology": "Short dated durable events only; merge repetitions; preserve causality and unresolved consequences.",
            "characters": "For EACH character present in personal_memory_records, summarize only that character's own knowledge/experiences/dialogue memory. Never import chronology or another character's memory as personal knowledge.",
            "relationship_events": "Only durable relationship changes evidenced by relationship_updates/scene summaries.",
            "thread_events": "Only plot-thread changes evidenced by thread updates/scene summaries.",
            "state_evidence": "Facts useful for reconstructing the true end-state; do not guess.",
        },
        "required_summary_shape": {
            "chronology": [],
            "characters": {},
            "relationship_events": [],
            "thread_events": [],
            "state_evidence": [],
        },
        "instruction": "Semantic compaction block. Scene prose is intentionally omitted. Use scene summaries, durable extracted updates, chronology and strictly personal memory. Preserve facts, not wording. No invented facts.",
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [raw[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(raw), storage.MAX_PACKET_CHARS)] or ["{}"]
    read = {
        "kind": "block",
        "read_id": secrets.token_urlsafe(12),
        "block_index": block_index,
        "chunks": chunks,
        "read_chunks": [0] if chunks else [],
    }
    migration["active_read"] = read
    _save_migration(session_id, migration)
    return {
        "ok": True,
        "read_id": read["read_id"],
        "block_index": block_index,
        "turn_range": [start, end],
        "chunk_count": len(chunks),
        "first_chunk_included": True,
        "content": chunks[0],
        "next_chunk_index": 1 if len(chunks) > 1 else None,
    }


def get_continuation_read_chunk(session_id: str, migration_id: str, read_id: str, chunk_index: int) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    _validate_migration(migration, migration_id)
    read = migration.get("active_read") if isinstance(migration.get("active_read"), dict) else {}
    if str(read.get("read_id") or "") != str(read_id):
        raise PermissionError("INVALID_CONTINUATION_READ")
    chunks = read.get("chunks") if isinstance(read.get("chunks"), list) else []
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CONTINUATION_CHUNK_OUT_OF_RANGE")
    seen = set(read.get("read_chunks", []))
    seen.add(chunk_index)
    read["read_chunks"] = sorted(seen)
    migration["active_read"] = read
    _save_migration(session_id, migration)
    return {
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": len(seen) == len(chunks),
        "next_chunk_index": chunk_index + 1 if chunk_index + 1 < len(chunks) else None,
    }


def _require_read_complete(migration: Dict[str, Any], *, kind: str, block_index: int | None = None) -> None:
    read = migration.get("active_read") if isinstance(migration.get("active_read"), dict) else {}
    if read.get("kind") != kind:
        raise RuntimeError("CONTINUATION_READ_REQUIRED")
    if block_index is not None and int(read.get("block_index", -1)) != int(block_index):
        raise RuntimeError("CONTINUATION_READ_REQUIRED")
    chunks = read.get("chunks") if isinstance(read.get("chunks"), list) else []
    if len(set(read.get("read_chunks", []))) < len(chunks):
        raise RuntimeError("CONTINUATION_READ_INCOMPLETE")


def commit_continuation_block(session_id: str, migration_id: str, block_index: int, summary: Dict[str, Any]) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    _validate_migration(migration, migration_id)
    _block_range(migration, block_index)
    _require_read_complete(migration, kind="block", block_index=block_index)
    if not isinstance(summary, dict) or not isinstance(summary.get("chronology", []), list) or not isinstance(summary.get("characters", {}), dict):
        raise ValueError("CONTINUATION_BLOCK_SUMMARY_INVALID")
    if len(json.dumps(summary, ensure_ascii=False)) > 180000:
        raise ValueError("CONTINUATION_BLOCK_SUMMARY_TOO_LARGE")
    summaries = migration.setdefault("block_summaries", {})
    summaries[str(block_index)] = deepcopy(summary)
    migration["active_read"] = None
    migration["final_package"] = None
    _save_migration(session_id, migration)
    done = {int(k) for k in summaries}
    next_block = next((i for i in range(int(migration["block_count"])) if i not in done), None)
    return {
        "ok": True,
        "completed_block": block_index,
        "next_block_index": next_block,
        "ready_for_finalization": next_block is None,
    }


def prepare_continuation_final_read(session_id: str, migration_id: str) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    _validate_migration(migration, migration_id)
    expected = set(range(int(migration["block_count"])))
    done = {int(k) for k in migration.get("block_summaries", {})}
    if done != expected:
        raise RuntimeError("CONTINUATION_BLOCKS_INCOMPLETE")
    p = _load_source_parts(session_id)
    payload = {
        "migration_id": migration_id,
        "source_turn": migration["source_turn"],
        "block_summaries": [deepcopy(migration["block_summaries"][str(i)]) for i in range(int(migration["block_count"]))],
        "current_state_raw": deepcopy(p["state"]),
        "recent_turns_exact": deepcopy(p["turns"][-RECENT_TURN_COUNT:]),
        "recent_scene_summaries": deepcopy(p["scenes"][-RECENT_SCENE_COUNT:]),
        "character_registry": [
            {"character_id": storage._card_id(c), "name": storage._card_name(c), "role": storage._card_role(c)}
            for c in p["cards"] if storage._card_id(c)
        ],
        "relationship_state_raw": {
            "relationships": deepcopy(p["state"].get("relationships", {})),
            "relationship_documents": deepcopy(p["state"].get("relationship_documents", {})),
            "relationship_schemas": deepcopy(p["state"].get("relationship_schemas", {})),
        },
        "required_package_shape": {
            "chronology": [{"date": "story date", "summary": "durable history", "participants": [], "importance": "normal|major|anchor|critical"}],
            "characters": {"CHARACTER_ID": {"knowledge": [], "experiences": [], "dialogue_memory": []}},
            "current": {},
            "threads": {},
        },
        "final_rules": [
            "Merge block summaries globally; remove repetition but preserve distinct facts and causal order.",
            "Character memory remains strictly per-character. Never add a fact merely because chronology or another character knows it.",
            "Reconstruct current from the latest exact turns, not stale current_state_raw fields.",
            "Close or update stale threads according to actual later events; preserve genuinely unresolved questions.",
            "Do not alter relationship numeric/document stores here; they are copied from persistent current state.",
            "Do not invent facts. If evidence conflicts, prefer the latest exact committed turn and preserve uncertainty where unresolved.",
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [raw[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(raw), storage.MAX_PACKET_CHARS)] or ["{}"]
    read = {"kind": "final", "read_id": secrets.token_urlsafe(12), "chunks": chunks, "read_chunks": [0]}
    migration["active_read"] = read
    _save_migration(session_id, migration)
    return {
        "ok": True,
        "read_id": read["read_id"],
        "chunk_count": len(chunks),
        "first_chunk_included": True,
        "content": chunks[0],
        "next_chunk_index": 1 if len(chunks) > 1 else None,
    }


def _as_text_list(value: Any) -> List[str]:
    values = value if isinstance(value, list) else []
    result: List[str] = []
    seen = set()
    for item in values:
        if isinstance(item, dict):
            text = item.get("text") or item.get("fact") or item.get("summary") or item.get("event") or item.get("topic")
        else:
            text = item
        text = " ".join(str(text or "").split()).strip()
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _normalized_compact_memory(source: Dict[str, Any], cards: List[Dict[str, Any]], package: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"characters": {}}
    character_data = package.get("characters") if isinstance(package.get("characters"), dict) else {}
    is_v5 = False
    try:
        is_v5 = int(source.get("version", 1) or 1) >= 5
    except (TypeError, ValueError):
        pass
    is_v5 = is_v5 or isinstance(source.get("profile_schema"), dict)
    for card in cards:
        cid = storage._card_id(card)
        raw = character_data.get(cid, {}) if isinstance(character_data.get(cid), dict) else {}
        knowledge = _as_text_list(raw.get("knowledge"))
        experiences = _as_text_list(raw.get("experiences"))
        dialogue = _as_text_list(raw.get("dialogue_memory"))
        bucket = {"knowledge_journal": [], "knowledge": [], "experiences": [], "dialogue_memory": []}
        if is_v5:
            bucket["knowledge_journal"] = [
                {"entry_id": f"continuation_{cid}_journal_{i}", "text": text, "turn": 0}
                for i, text in enumerate(knowledge, 1)
            ]
        else:
            bucket["knowledge"] = [
                {"fact_id": f"continuation_{cid}_fact_{i}", "fact": text, "learned_turn": 0, "confidence": "certain", "compacted_from_prior_session": True}
                for i, text in enumerate(knowledge, 1)
            ]
        bucket["experiences"] = [
            {"event_id": f"continuation_{cid}_exp_{i}", "summary": text, "turn": 0, "compacted_from_prior_session": True}
            for i, text in enumerate(experiences, 1)
        ]
        bucket["dialogue_memory"] = [
            {"topic_id": f"continuation_{cid}_dialogue_{i}", "summary": text, "turn": 0, "compacted_from_prior_session": True}
            for i, text in enumerate(dialogue, 1)
        ]
        result["characters"][cid] = bucket
    return result


def _normalized_chronology(package: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    values = package.get("chronology") if isinstance(package.get("chronology"), list) else []
    for i, raw in enumerate(values, 1):
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("summary") or raw.get("event") or "").split()).strip()
        if not text:
            continue
        importance = str(raw.get("importance") or "normal").casefold()
        if importance not in {"normal", "major", "anchor", "critical"}:
            importance = "normal"
        participants = raw.get("participants") if isinstance(raw.get("participants"), list) else []
        row = {
            "event_id": f"continuation_history_{i}",
            "turn_number": 0,
            "story_date": raw.get("date") or raw.get("story_date"),
            "event": text,
            "importance": importance,
            "participants_present": [str(x) for x in participants if x],
            "compacted_from_prior_session": True,
        }
        result.append({k: v for k, v in row.items() if v not in (None, "", [])})
    return result


def commit_continuation_final(session_id: str, migration_id: str, package: Dict[str, Any]) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    _validate_migration(migration, migration_id)
    _require_read_complete(migration, kind="final")
    if not isinstance(package, dict):
        raise ValueError("CONTINUATION_FINAL_PACKAGE_INVALID")
    if not isinstance(package.get("chronology"), list) or not isinstance(package.get("characters"), dict):
        raise ValueError("CONTINUATION_FINAL_PACKAGE_INVALID")
    if not isinstance(package.get("current"), dict) or not isinstance(package.get("threads"), (dict, list)):
        raise ValueError("CONTINUATION_FINAL_PACKAGE_INVALID")
    p = _load_source_parts(session_id)
    valid_ids = {storage._card_id(c) for c in p["cards"]}
    unknown = set(package["characters"]) - valid_ids
    if unknown:
        raise ValueError("CONTINUATION_UNKNOWN_CHARACTER")
    normalized = deepcopy(package)
    normalized["memory_normalized"] = _normalized_compact_memory(p["source"], p["cards"], package)
    normalized["chronology_normalized"] = _normalized_chronology(package)
    migration["final_package"] = normalized
    migration["active_read"] = None
    _save_migration(session_id, migration)
    return {
        "ok": True,
        "ready_to_create": True,
        "source_turn": migration["source_turn"],
        "chronology_count": len(normalized["chronology_normalized"]),
        "memory_counts": _memory_counts(normalized["memory_normalized"]),
        "instruction": "Now createContinuationSession may be called. Source session remains untouched.",
    }


def create_continuation_session(session_id: str) -> Dict[str, Any]:
    migration = _load_migration(session_id)
    package = migration.get("final_package") if isinstance(migration.get("final_package"), dict) else None
    if not package:
        raise RuntimeError("CONTINUATION_FINALIZATION_REQUIRED")
    p = _load_source_parts(session_id)
    current_turn = int(migration["source_turn"])
    source = deepcopy(p["source"])
    source["characters"] = deepcopy(p["cards"])

    state = deepcopy(p["state"])
    state["current"] = deepcopy(package["current"])
    state["threads"] = deepcopy(package["threads"])
    state.setdefault("world", {})
    if isinstance(state["world"], dict):
        state["world"]["continuation_origin"] = {"session_id": session_id, "source_turn": current_turn}

    source["starting_state"] = deepcopy(state)
    source["continuation"] = {
        "continuation_of_session_id": session_id,
        "source_turn": current_turn,
        "compaction_version": MIGRATION_VERSION,
        "history_contract": "Prior events are compacted canon. Character memory is strictly personal. Exact bridge turns are prior context and must not be replayed.",
    }

    new_meta = storage.create_session(
        source,
        meta_patch={
            "continuation_of_session_id": session_id,
            "continuation_source_turn": current_turn,
            "continuation_compaction_version": MIGRATION_VERSION,
        },
    )
    new_id = str(new_meta["session_id"])
    new_root = storage.SESSIONS_DIR / new_id
    recent_turns = deepcopy(p["turns"][-RECENT_TURN_COUNT:])
    write_batch(
        new_root,
        {
            "source.json": json_text(source),
            "characters.json": json_text(p["cards"]),
            "state.json": json_text(state),
            "memory.json": json_text(package["memory_normalized"]),
            "chronology.json": json_text(package["chronology_normalized"]),
            "handoff_tail.json": json_text(recent_turns),
        },
    )
    return {
        "ok": True,
        "session_id": new_id,
        "continuation_of_session_id": session_id,
        "continuation_source_turn": current_turn,
        "new_turn_number": 0,
        "chronology_count": len(package["chronology_normalized"]),
        "memory_counts": _memory_counts(package["memory_normalized"]),
        "recent_turn_bridge": len(recent_turns),
        "instruction": f"Continue with session {new_id}; do not replay prior bridge turns.",
    }
