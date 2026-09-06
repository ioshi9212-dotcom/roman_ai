from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from . import storage
from .character_registry import refresh_pov_familiarity
from .game_day import sync_game_day
from .relationship_runtime import repair_relationship_state
from .rollback_snapshot_runtime import SNAPSHOT_FILE
from .session_runtime import _canonicalize_state_character_refs, _resolve_character_id
from .stability_runtime import (
    _clean_scene_pointer,
    _merge_state_patch_exact_relationships,
    _scene_header_current,
    _turns_text,
)
from .transactional_storage import json_text, session_transaction, write_batch


class RollbackError(RuntimeError):
    pass


def _initial_replay_state(source: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    cards = storage._normalise_cards(source.get("characters", []))
    state = storage._template(
        "state.json",
        {"current": {}, "pov": {}, "characters": {}, "relationships": {}, "threads": {}, "world": {}},
    )
    starting_state = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    state = storage._deep_merge(state, starting_state)

    pov_id = storage._find_pov_id(source, cards)
    if not isinstance(state.get("pov"), dict):
        state["pov"] = {}
    if pov_id and not state["pov"].get("character_id"):
        state["pov"]["character_id"] = pov_id
    if isinstance(source.get("world"), dict):
        state["world"] = storage._deep_merge(
            source.get("world", {}),
            state.get("world", {}) if isinstance(state.get("world"), dict) else {},
        )

    memory = storage._normalise_memory(storage._template("memory.json", {"characters": {}}))
    for card in cards:
        cid = storage._card_id(card)
        if cid:
            storage._memory_bucket(memory, cid)
    chronology = storage._template("chronology.json", [])
    if not isinstance(chronology, list):
        chronology = []
    state = storage._refresh_runtime_presence(state, cards, 0)
    return cards, state, memory, chronology


def _refresh_derived_state(
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: List[Dict[str, Any]],
    turns: List[Dict[str, Any]],
    turn_number: int,
) -> Dict[str, Any]:
    state = _canonicalize_state_character_refs(cards, state)
    state = repair_relationship_state(
        state,
        source=source,
        turns=turns,
        cards=cards,
        resolve_character_id=_resolve_character_id,
    )
    return refresh_pov_familiarity(cards, state, memory, chronology, turns, turn_number)


def _apply_saved_turn(
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: List[Dict[str, Any]],
    turns_so_far: List[Dict[str, Any]],
    turn: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    turn_number = int(turn.get("turn_number", 0) or 0)
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}

    cards = storage._apply_character_upserts(cards, extracted)
    state = _merge_state_patch_exact_relationships(state, extracted.get("state_patch"))
    header_current = _scene_header_current(str(turn.get("scene_output") or ""))
    if header_current:
        current = state.get("current") if isinstance(state.get("current"), dict) else {}
        state["current"] = storage._deep_merge(current, header_current)
    state = _clean_scene_pointer(state, extracted)
    state = sync_game_day(state, source)
    state = storage._refresh_runtime_presence(state, cards, turn_number)

    memory = storage._normalise_memory(memory)
    for card in cards:
        cid = storage._card_id(card)
        if cid:
            storage._memory_bucket(memory, cid)
    memory = storage._apply_memory_events(memory, extracted, turn_number)

    if isinstance(extracted.get("chronology"), list):
        chronology = [*chronology, *deepcopy(extracted["chronology"])]

    turns_now = [*turns_so_far, deepcopy(turn)]
    state = _refresh_derived_state(source, cards, state, memory, chronology, turns_now, turn_number)
    return cards, state, memory, chronology


def _apply_saved_audit(
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: List[Dict[str, Any]],
    turns_so_far: List[Dict[str, Any]],
    audit: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    end_turn = int(audit.get("end_turn", 0) or 0)
    repairs = audit.get("repairs") if isinstance(audit.get("repairs"), dict) else {}
    state = _merge_state_patch_exact_relationships(state, repairs.get("state_patch"))
    state = _clean_scene_pointer(state, repairs)
    state = sync_game_day(state, source)
    memory = storage._apply_memory_events(storage._normalise_memory(memory), repairs, end_turn)
    if isinstance(repairs.get("chronology_add"), list):
        chronology = [*chronology, *deepcopy(repairs["chronology_add"])]
    state = _refresh_derived_state(source, cards, state, memory, chronology, turns_so_far, end_turn)
    return state, memory, chronology


def _replay_through(
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    target_turn: int,
) -> Dict[str, Any]:
    cards, state, memory, chronology = _initial_replay_state(source)
    turns_so_far: List[Dict[str, Any]] = []
    audits_by_end: Dict[int, List[Dict[str, Any]]] = {}
    for audit in audits:
        if not isinstance(audit, dict):
            continue
        end_turn = int(audit.get("end_turn", 0) or 0)
        if end_turn <= target_turn:
            audits_by_end.setdefault(end_turn, []).append(audit)

    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_number = int(turn.get("turn_number", 0) or 0)
        if turn_number <= 0 or turn_number > target_turn:
            continue
        cards, state, memory, chronology = _apply_saved_turn(
            source, cards, state, memory, chronology, turns_so_far, turn
        )
        turns_so_far.append(deepcopy(turn))
        for audit in audits_by_end.get(turn_number, []):
            state, memory, chronology = _apply_saved_audit(
                source, cards, state, memory, chronology, turns_so_far, audit
            )

    return {
        "characters": cards,
        "state": state,
        "memory": memory,
        "chronology": chronology,
        "turns": turns_so_far,
        "audits": [deepcopy(a) for a in audits if isinstance(a, dict) and int(a.get("end_turn", 0) or 0) <= target_turn],
    }


def _mismatch_blocks(root, replayed: Dict[str, Any]) -> List[str]:
    live = {
        "characters": storage._read_json(root / "characters.json", []),
        "state": storage._read_json(root / "state.json", {}),
        "memory": storage._normalise_memory(storage._read_json(root / "memory.json", {})),
        "chronology": storage._read_json(root / "chronology.json", []),
    }
    result = []
    for key in ("characters", "state", "memory", "chronology"):
        if live[key] != replayed[key]:
            result.append(key)
    return result


def _restored_meta(live_meta: Dict[str, Any], target_turn: int, audits: List[Dict[str, Any]]) -> Dict[str, Any]:
    meta = deepcopy(live_meta)
    last_audit = max(
        (int(a.get("end_turn", 0) or 0) for a in audits if isinstance(a, dict) and int(a.get("end_turn", 0) or 0) <= target_turn),
        default=0,
    )
    meta["turn_number"] = target_turn
    meta["last_audit_turn"] = last_audit
    meta["audit_required"] = bool(target_turn > 0 and target_turn % 15 == 0 and last_audit < target_turn)
    meta["handoff_required"] = False
    meta["last_rollback_at"] = datetime.now(timezone.utc).isoformat()
    return meta


def _write_restored_state(
    root,
    *,
    target_turn: int,
    turns: List[Dict[str, Any]],
    characters: Any,
    state: Any,
    memory: Any,
    chronology: Any,
    audits: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> None:
    write_batch(
        root,
        {
            "turns.jsonl": _turns_text(turns),
            "characters.json": json_text(characters),
            "state.json": json_text(state),
            "memory.json": json_text(memory),
            "chronology.json": json_text(chronology),
            "audits.json": json_text(audits),
            "meta.json": json_text(meta),
        },
    )
    for name in (
        "turn_packet.json",
        "audit_packet.json",
        "handoff_tail.json",
        "resume_token.json",
    ):
        (root / name).unlink(missing_ok=True)


def rollback_last_turn(session_id: str, expected_turn_number: int, confirm: bool) -> Dict[str, Any]:
    if confirm is not True:
        raise RollbackError("ROLLBACK_CONFIRMATION_REQUIRED")
    expected_turn_number = int(expected_turn_number)
    if expected_turn_number < 1:
        raise RollbackError("ROLLBACK_TURN_INVALID")

    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        current_turn = int(meta.get("turn_number", 0) or 0)
        if current_turn != expected_turn_number:
            raise RollbackError("ROLLBACK_EXPECTED_TURN_MISMATCH")

        turns = storage._read_turns(root)
        if not turns or int(turns[-1].get("turn_number", 0) or 0) != expected_turn_number:
            raise RollbackError("ROLLBACK_LAST_TURN_NOT_FOUND")
        target_turn = expected_turn_number - 1
        remaining_turns = turns[:-1]

        snapshot = storage._read_json(root / SNAPSHOT_FILE, {})
        if (
            isinstance(snapshot, dict)
            and int(snapshot.get("committed_turn", 0) or 0) == expected_turn_number
            and int(snapshot.get("previous_turn", -1) or -1) == target_turn
        ):
            previous_meta = deepcopy(snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {})
            previous_meta["last_rollback_at"] = datetime.now(timezone.utc).isoformat()
            _write_restored_state(
                root,
                target_turn=target_turn,
                turns=remaining_turns,
                characters=deepcopy(snapshot.get("characters", [])),
                state=deepcopy(snapshot.get("state", {})),
                memory=deepcopy(snapshot.get("memory", {})),
                chronology=deepcopy(snapshot.get("chronology", [])),
                audits=deepcopy(snapshot.get("audits", [])),
                meta=previous_meta,
            )
            (root / SNAPSHOT_FILE).unlink(missing_ok=True)
            return {
                "ok": True,
                "rolled_back_turn": expected_turn_number,
                "turn_number": target_turn,
                "method": "exact_pre_turn_snapshot",
                "ready_to_retry_turn": expected_turn_number,
            }

        # Sessions committed before rollback snapshots existed can still be repaired,
        # but only if deterministic replay reproduces the current canonical files.
        source = storage._read_json(root / "source.json", {})
        audits = storage._read_json(root / "audits.json", [])
        if not isinstance(audits, list):
            audits = []
        replay_current = _replay_through(source, turns, audits, expected_turn_number)
        mismatches = _mismatch_blocks(root, replay_current)
        if mismatches:
            raise RollbackError("ROLLBACK_REPLAY_MISMATCH:" + ",".join(mismatches))

        replay_previous = _replay_through(source, remaining_turns, audits, target_turn)
        target_audits = replay_previous["audits"]
        restored_meta = _restored_meta(meta, target_turn, target_audits)
        _write_restored_state(
            root,
            target_turn=target_turn,
            turns=replay_previous["turns"],
            characters=replay_previous["characters"],
            state=replay_previous["state"],
            memory=replay_previous["memory"],
            chronology=replay_previous["chronology"],
            audits=target_audits,
            meta=restored_meta,
        )
        (root / SNAPSHOT_FILE).unlink(missing_ok=True)
        return {
            "ok": True,
            "rolled_back_turn": expected_turn_number,
            "turn_number": target_turn,
            "method": "verified_historical_replay",
            "ready_to_retry_turn": expected_turn_number,
        }
