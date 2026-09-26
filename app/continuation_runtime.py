from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .transactional_storage import json_text, write_batch
from .scene_compaction_runtime import load_scene_history


RECENT_TURN_COUNT = 15
RECENT_SCENE_COUNT = 8


def _text_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _row_key(row: Dict[str, Any], id_fields: tuple[str, ...], text_fields: tuple[str, ...]) -> str:
    for field in id_fields:
        value = row.get(field)
        if value not in (None, ""):
            return f"id:{field}:{value}"
    for field in text_fields:
        value = row.get(field)
        if value not in (None, ""):
            return f"text:{field}:{_text_key(value)}"
    return repr(sorted(row.items(), key=lambda item: str(item[0])))


def _dedupe_rows(
    rows: Any,
    *,
    id_fields: tuple[str, ...],
    text_fields: tuple[str, ...],
) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    kept: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = deepcopy(raw)
        key = _row_key(row, id_fields, text_fields)
        if key not in kept:
            order.append(key)
        kept[key] = row
    return [kept[key] for key in order]


def compact_memory(memory: Any) -> Dict[str, Any]:
    normalized = storage._normalise_memory(deepcopy(memory if isinstance(memory, dict) else {}))
    characters = normalized.get("characters") if isinstance(normalized.get("characters"), dict) else {}
    result: Dict[str, Any] = {"characters": {}}

    for character_id, raw_bucket in characters.items():
        bucket = raw_bucket if isinstance(raw_bucket, dict) else {}
        result["characters"][str(character_id)] = {
            "knowledge_journal": _dedupe_rows(
                bucket.get("knowledge_journal"),
                id_fields=("entry_id",),
                text_fields=("text", "fact", "summary"),
            ),
            "knowledge": _dedupe_rows(
                bucket.get("knowledge"),
                id_fields=("fact_id",),
                text_fields=("fact", "text", "summary"),
            ),
            "experiences": _dedupe_rows(
                bucket.get("experiences"),
                id_fields=("event_id",),
                text_fields=("event", "text", "summary", "description"),
            ),
            "dialogue_memory": _dedupe_rows(
                bucket.get("dialogue_memory"),
                id_fields=("topic_id",),
                text_fields=("topic", "text", "summary"),
            ),
        }
    return result


def compact_chronology(chronology: Any) -> List[Dict[str, Any]]:
    if not isinstance(chronology, list):
        return []
    kept: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in chronology:
        if not isinstance(raw, dict):
            continue
        row = deepcopy(raw)
        event_id = str(row.get("event_id") or "").strip()
        if event_id:
            key = f"id:{event_id}"
        else:
            turn = row.get("turn_number") or row.get("turn") or ""
            date = row.get("story_date") or row.get("date") or ""
            text = row.get("event") or row.get("summary") or row.get("text") or row.get("description") or ""
            key = f"event:{turn}:{date}:{_text_key(text)}"
        if key not in kept:
            order.append(key)
        kept[key] = row
    return [kept[key] for key in order]


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


def _load_source_parts(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    meta = storage._read_json(root / "meta.json", {})
    turns = storage._read_turns(root)
    scenes = load_scene_history(root)

    return {
        "root": root,
        "source": source,
        "cards": cards,
        "state": state,
        "memory": memory,
        "chronology": chronology,
        "meta": meta,
        "turns": turns,
        "scenes": scenes,
    }


def build_continuation_preview(session_id: str) -> Dict[str, Any]:
    parts = _load_source_parts(session_id)
    compacted_memory = compact_memory(parts["memory"])
    compacted_chronology = compact_chronology(parts["chronology"])
    current_turn = int(parts["meta"].get("turn_number") or len(parts["turns"]) or 0)
    recent_turns = deepcopy(parts["turns"][-RECENT_TURN_COUNT:])
    recent_scenes = deepcopy(parts["scenes"][-RECENT_SCENE_COUNT:])

    return {
        "ok": True,
        "read_only": True,
        "source_session_id": session_id,
        "source_turn": current_turn,
        "strategy": {
            "old_session_immutable": True,
            "copy_live_character_cards": True,
            "copy_current_state_and_relationships": True,
            "memory_compaction": "lossless exact-id/text dedupe only; no semantic facts are discarded",
            "chronology_compaction": "preserve existing macro chronology and remove exact duplicate events only",
            "recent_turn_bridge": RECENT_TURN_COUNT,
            "recent_scene_bridge": RECENT_SCENE_COUNT,
            "turn_counter_restarts_in_new_session": True,
        },
        "counts": {
            "characters": len(parts["cards"]),
            "source_turns": len(parts["turns"]),
            "chronology_before": len(parts["chronology"]) if isinstance(parts["chronology"], list) else 0,
            "chronology_after": len(compacted_chronology),
            "memory_before": _memory_counts(parts["memory"]),
            "memory_after": _memory_counts(compacted_memory),
            "recent_turns_kept_exact": len(recent_turns),
            "recent_scenes_kept": len(recent_scenes),
        },
        "current": deepcopy(parts["state"].get("current", {})) if isinstance(parts["state"], dict) else {},
        "pov": deepcopy(parts["state"].get("pov", {})) if isinstance(parts["state"], dict) else {},
        "open_threads": deepcopy(parts["state"].get("threads", {})) if isinstance(parts["state"], dict) else {},
        "continuation_contract": {
            "chronology_is_author_truth_not_character_knowledge": True,
            "personal_knowledge_stays_per_character": True,
            "relationships_keep_current_values_and_documents": True,
            "raw_old_turns_remain_only_in_source_session": True,
            "source_session_remains_recoverable": True,
        },
        "instruction": (
            "Preview only. If this looks correct, create a continuation session from this exact source session. "
            "The old session is never modified."
        ),
    }


def create_continuation_session(session_id: str) -> Dict[str, Any]:
    parts = _load_source_parts(session_id)
    current_turn = int(parts["meta"].get("turn_number") or len(parts["turns"]) or 0)
    compacted_memory = compact_memory(parts["memory"])
    compacted_chronology = compact_chronology(parts["chronology"])
    recent_turns = deepcopy(parts["turns"][-RECENT_TURN_COUNT:])
    recent_scenes = deepcopy(parts["scenes"][-RECENT_SCENE_COUNT:])

    source = deepcopy(parts["source"])
    source["characters"] = deepcopy(parts["cards"])
    source["starting_state"] = deepcopy(parts["state"])
    source["continuation"] = {
        "continuation_of_session_id": session_id,
        "source_turn": current_turn,
        "recent_scene_bridge": [
            {
                key: deepcopy(scene.get(key))
                for key in ("scene_id", "start_turn", "end_turn", "summary", "participants", "locations")
                if scene.get(key) not in (None, "", [], {})
            }
            for scene in recent_scenes
            if isinstance(scene, dict)
        ],
        "history_contract": (
            "Events before the new session live in chronology and the exact recent-turn bridge. "
            "They are prior canon, not events that happened at new-session turn 0."
        ),
    }

    new_meta = storage.create_session(
        source,
        meta_patch={
            "continuation_of_session_id": session_id,
            "continuation_source_turn": current_turn,
            "continuation_compaction_version": 1,
        },
    )
    new_id = str(new_meta["session_id"])
    new_root = storage.SESSIONS_DIR / new_id

    state = deepcopy(parts["state"])
    state.setdefault("world", {})
    if isinstance(state["world"], dict):
        state["world"]["continuation_origin"] = {
            "session_id": session_id,
            "source_turn": current_turn,
        }

    handoff_tail = recent_turns
    write_batch(
        new_root,
        {
            "source.json": json_text(source),
            "characters.json": json_text(parts["cards"]),
            "state.json": json_text(state),
            "memory.json": json_text(compacted_memory),
            "chronology.json": json_text(compacted_chronology),
            "handoff_tail.json": json_text(handoff_tail),
        },
    )

    return {
        "ok": True,
        "session_id": new_id,
        "continuation_of_session_id": session_id,
        "continuation_source_turn": current_turn,
        "new_turn_number": 0,
        "counts": {
            "characters": len(parts["cards"]),
            "chronology": len(compacted_chronology),
            "memory": _memory_counts(compacted_memory),
            "recent_turns_kept_exact": len(recent_turns),
            "recent_scenes_kept": len(recent_scenes),
        },
        "instruction": (
            f"Continue with session {new_id}. It is a new technical session but a direct canonical continuation "
            f"of {session_id} through source turn {current_turn}. Do not replay the bridge turns; they are prior context."
        ),
    }
