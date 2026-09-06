from __future__ import annotations

from typing import Any, Dict, List

from . import storage
from .rollback_snapshot_runtime import SNAPSHOT_FILE
from .turn_rollback import _mismatch_blocks, _replay_through


def _count_list(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _ids_from_rows(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        cid = row.get("character_id") or row.get("id") or row.get("owner_character_id")
        if cid:
            result.append(str(cid))
    return sorted(set(result))


def _leaf_paths(value: Any, prefix: str = "") -> List[str]:
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    result: List[str] = []
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict):
            nested = _leaf_paths(child, path)
            result.extend(nested or [path])
        else:
            result.append(path)
    return sorted(result)


def rollback_diagnostics(session_id: str) -> Dict[str, Any]:
    """Read-only structural diagnostics for the current latest turn."""
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0)
    turns = storage._read_turns(root)
    audits = storage._read_json(root / "audits.json", [])
    if not isinstance(audits, list):
        audits = []
    source = storage._read_json(root / "source.json", {})
    last_turn = turns[-1] if turns else {}
    extracted = last_turn.get("extracted") if isinstance(last_turn.get("extracted"), dict) else {}
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}

    transactions_root = root / ".transactions"
    transaction_dirs = []
    if transactions_root.exists():
        transaction_dirs = sorted(path.name for path in transactions_root.iterdir() if path.is_dir())

    replay_mismatch: List[str] = []
    replay_error = None
    if current_turn > 0 and turns:
        try:
            replay_current = _replay_through(source, turns, audits, current_turn)
            replay_mismatch = _mismatch_blocks(root, replay_current)
        except Exception as exc:  # diagnostic only
            replay_error = f"{type(exc).__name__}:{exc}"

    chronology_add = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
    current_chronology = storage._read_json(root / "chronology.json", [])
    chronology_suffix_matches = bool(
        chronology_add
        and isinstance(current_chronology, list)
        and len(current_chronology) >= len(chronology_add)
        and current_chronology[-len(chronology_add):] == chronology_add
    )

    snapshot = storage._read_json(root / SNAPSHOT_FILE, {})
    snapshot_for_current = bool(
        isinstance(snapshot, dict)
        and int(snapshot.get("committed_turn", 0) or 0) == current_turn
    )

    return {
        "read_only": True,
        "session_id": session_id,
        "current_turn": current_turn,
        "last_saved_turn": int(last_turn.get("turn_number", 0) or 0) if isinstance(last_turn, dict) else 0,
        "snapshot": {
            "exists": (root / SNAPSHOT_FILE).exists(),
            "matches_current_turn": snapshot_for_current,
            "committed_turn": int(snapshot.get("committed_turn", 0) or 0) if isinstance(snapshot, dict) else 0,
            "previous_turn": snapshot.get("previous_turn") if isinstance(snapshot, dict) else None,
        },
        "transaction_artifacts": {
            "transactions_root_exists": transactions_root.exists(),
            "transaction_dir_count": len(transaction_dirs),
            "transaction_dir_names": transaction_dirs[:10],
        },
        "historical_replay": {
            "mismatch_blocks": replay_mismatch,
            "error": replay_error,
        },
        "last_turn_extracted": {
            "keys": sorted(extracted.keys()),
            "state_patch_top_level_keys": sorted(state_patch.keys()),
            "state_patch_leaf_paths": _leaf_paths(state_patch),
            "character_upserts_count": _count_list(extracted.get("character_upserts")),
            "character_upsert_ids": _ids_from_rows(extracted.get("character_upserts")),
            "chronology_count": len(chronology_add),
            "chronology_is_exact_current_suffix": chronology_suffix_matches,
            "knowledge_add_count": _count_list(extracted.get("knowledge_add")),
            "knowledge_character_ids": _ids_from_rows(extracted.get("knowledge_add")),
            "experiences_add_count": _count_list(extracted.get("experiences_add")),
            "experience_character_ids": _ids_from_rows(extracted.get("experiences_add")),
            "dialogue_memory_add_count": _count_list(extracted.get("dialogue_memory_add")),
            "dialogue_character_ids": _ids_from_rows(extracted.get("dialogue_memory_add")),
            "relationship_updates_count": _count_list(extracted.get("relationship_updates")),
            "relationship_character_ids": _ids_from_rows(extracted.get("relationship_updates")),
            "presence_updates_count": _count_list(extracted.get("presence_updates")),
            "presence_character_ids": _ids_from_rows(extracted.get("presence_updates")),
        },
    }
