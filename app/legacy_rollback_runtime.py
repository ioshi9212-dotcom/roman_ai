from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

from . import storage, turn_rollback
from .character_registry import refresh_pov_familiarity
from .rollback_snapshot_runtime import SNAPSHOT_FILE, SNAPSHOT_VERSION, build_pre_turn_snapshot
from .stability_runtime import (
    _clean_scene_pointer,
    _merge_state_patch_exact_relationships,
    _scene_header_current,
)
from .transactional_storage import session_transaction


_ORIGINAL_ROLLBACK = None
_MISSING = object()
_RELATIONSHIP_KEYS = {"relationships", "relationship_documents"}
_HEADER_KEYS = {"date", "time", "location", "game_day"}
_TRANSIENT_CURRENT_KEYS = {"entered_characters", "left_characters"}


class LegacyRollbackUnsafe(RuntimeError):
    pass


def _get_path(value: Any, path: Tuple[str, ...]) -> tuple[bool, Any]:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, deepcopy(current)


def _set_path(value: Dict[str, Any], path: Tuple[str, ...], new_value: Any) -> None:
    current = value
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = deepcopy(new_value)


def _delete_path(value: Dict[str, Any], path: Tuple[str, ...]) -> None:
    chain: List[tuple[Dict[str, Any], str]] = []
    current: Any = value
    for key in path[:-1]:
        if not isinstance(current, dict) or key not in current:
            return
        chain.append((current, key))
        current = current[key]
    if isinstance(current, dict):
        current.pop(path[-1], None)
    for parent, key in reversed(chain):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
        else:
            break


def _leaf_paths(value: Any, prefix: Tuple[str, ...] = ()) -> Iterable[Tuple[str, ...]]:
    if not isinstance(value, dict):
        if prefix:
            yield prefix
        return
    if not value:
        if prefix:
            yield prefix
        return
    for key, child in value.items():
        path = (*prefix, str(key))
        if isinstance(child, dict) and child:
            yield from _leaf_paths(child, path)
        else:
            yield path


def _initial_cards(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    return storage._normalise_cards(source.get("characters", []))


def _initial_state(source: Dict[str, Any]) -> Dict[str, Any]:
    cards = _initial_cards(source)
    state = storage._template(
        "state.json",
        {"current": {}, "pov": {}, "characters": {}, "relationships": {}, "threads": {}, "world": {}},
    )
    starting = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    state = storage._deep_merge(state, starting)
    pov_id = storage._find_pov_id(source, cards)
    if not isinstance(state.get("pov"), dict):
        state["pov"] = {}
    if pov_id and not state["pov"].get("character_id"):
        state["pov"]["character_id"] = pov_id
    if isinstance(source.get("world"), dict):
        world = state.get("world") if isinstance(state.get("world"), dict) else {}
        state["world"] = storage._deep_merge(source["world"], world)
    return storage._refresh_runtime_presence(state, cards, 0)


def _state_patch_from_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    return extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}


def _state_patch_from_audit(audit: Dict[str, Any]) -> Dict[str, Any]:
    repairs = audit.get("repairs") if isinstance(audit.get("repairs"), dict) else {}
    return repairs.get("state_patch") if isinstance(repairs.get("state_patch"), dict) else {}


def _latest_prior_state_value(
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    target_turn: int,
    path: Tuple[str, ...],
) -> tuple[bool, Any]:
    events: List[tuple[tuple[int, int], Dict[str, Any]]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        number = int(turn.get("turn_number", 0) or 0)
        if 0 < number <= target_turn:
            events.append(((number, 0), _state_patch_from_turn(turn)))
    for audit in audits:
        if not isinstance(audit, dict):
            continue
        number = int(audit.get("end_turn", 0) or 0)
        if 0 < number <= target_turn:
            events.append(((number, 1), _state_patch_from_audit(audit)))
    for _order, patch in sorted(events, key=lambda item: item[0], reverse=True):
        found, value = _get_path(patch, path)
        if found:
            return True, value
    return _get_path(_initial_state(source), path)


def _last_state_event_current_patch(
    turns: List[Dict[str, Any]], audits: List[Dict[str, Any]], target_turn: int
) -> Dict[str, Any]:
    candidates: List[tuple[tuple[int, int], Dict[str, Any]]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        number = int(turn.get("turn_number", 0) or 0)
        if number == target_turn:
            patch = _state_patch_from_turn(turn)
            current = patch.get("current") if isinstance(patch.get("current"), dict) else {}
            candidates.append(((number, 0), current))
    for audit in audits:
        if not isinstance(audit, dict):
            continue
        number = int(audit.get("end_turn", 0) or 0)
        if number == target_turn:
            patch = _state_patch_from_audit(audit)
            current = patch.get("current") if isinstance(patch.get("current"), dict) else {}
            candidates.append(((number, 1), current))
    if not candidates:
        return {}
    return deepcopy(max(candidates, key=lambda item: item[0])[1])


def _previous_header(
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    target_turn: int,
) -> Dict[str, Any]:
    if target_turn <= 0:
        current = _initial_state(source).get("current")
        return deepcopy(current) if isinstance(current, dict) else {}
    previous = next(
        (turn for turn in reversed(turns) if int(turn.get("turn_number", 0) or 0) == target_turn),
        None,
    )
    result = _scene_header_current(str(previous.get("scene_output") or "")) if isinstance(previous, dict) else {}
    # A completed audit at the same turn happens after the scene and may correct current.
    same_turn_audits = [
        audit
        for audit in audits
        if isinstance(audit, dict) and int(audit.get("end_turn", 0) or 0) == target_turn
    ]
    for audit in same_turn_audits:
        current = _state_patch_from_audit(audit).get("current")
        if not isinstance(current, dict):
            continue
        for key in _HEADER_KEYS:
            if key in current:
                result[key] = deepcopy(current[key])
    return result


def _memory_targets(extracted: Dict[str, Any]) -> List[tuple[str, str, str, str]]:
    result: List[tuple[str, str, str, str]] = []
    for item in extracted.get("knowledge_add", []) if isinstance(extracted.get("knowledge_add"), list) else []:
        if isinstance(item, dict) and item.get("character_id") and item.get("fact_id"):
            result.append((str(item["character_id"]), "knowledge", "fact_id", str(item["fact_id"])))
    for item in extracted.get("experiences_add", []) if isinstance(extracted.get("experiences_add"), list) else []:
        if isinstance(item, dict) and item.get("character_id") and item.get("event_id"):
            result.append((str(item["character_id"]), "experiences", "event_id", str(item["event_id"])))
    for item in extracted.get("dialogue_memory_add", []) if isinstance(extracted.get("dialogue_memory_add"), list) else []:
        if not isinstance(item, dict) or not item.get("topic_id"):
            continue
        participants = item.get("participants") or []
        if isinstance(participants, str):
            participants = [participants]
        for character_id in participants if isinstance(participants, list) else []:
            if character_id:
                result.append((str(character_id), "dialogue_memory", "topic_id", str(item["topic_id"])))
    return list(dict.fromkeys(result))


def _memory_before_turn(
    turns: List[Dict[str, Any]], audits: List[Dict[str, Any]], target_turn: int
) -> Dict[str, Any]:
    memory: Dict[str, Any] = {"characters": {}}
    audits_by_end: Dict[int, List[Dict[str, Any]]] = {}
    for audit in audits:
        if not isinstance(audit, dict):
            continue
        end_turn = int(audit.get("end_turn", 0) or 0)
        if 0 < end_turn <= target_turn:
            audits_by_end.setdefault(end_turn, []).append(audit)
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        number = int(turn.get("turn_number", 0) or 0)
        if number <= 0 or number > target_turn:
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        memory = storage._apply_memory_events(memory, extracted, number)
        for audit in audits_by_end.get(number, []):
            repairs = audit.get("repairs") if isinstance(audit.get("repairs"), dict) else {}
            memory = storage._apply_memory_events(memory, repairs, number)
    return storage._normalise_memory(memory)


def _find_memory_record(memory: Dict[str, Any], target: tuple[str, str, str, str]) -> Any:
    character_id, bucket_name, id_key, record_id = target
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    bucket = characters.get(character_id) if isinstance(characters.get(character_id), dict) else {}
    items = bucket.get(bucket_name) if isinstance(bucket.get(bucket_name), list) else []
    for item in items:
        if isinstance(item, dict) and str(item.get(id_key) or "") == record_id:
            return deepcopy(item)
    return _MISSING


def _restore_memory_record(memory: Dict[str, Any], target: tuple[str, str, str, str], prior: Any) -> None:
    character_id, bucket_name, id_key, record_id = target
    bucket = storage._memory_bucket(memory, character_id)
    items = bucket[bucket_name]
    index = next(
        (i for i, item in enumerate(items) if isinstance(item, dict) and str(item.get(id_key) or "") == record_id),
        None,
    )
    if prior is _MISSING:
        if index is not None:
            items.pop(index)
        return
    if index is None:
        items.append(deepcopy(prior))
    else:
        items[index] = deepcopy(prior)


def _revert_memory(
    live_memory: Dict[str, Any],
    last_extracted: Dict[str, Any],
    turns: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    target_turn: int,
    expected_turn: int,
) -> Dict[str, Any]:
    candidate = storage._normalise_memory(deepcopy(live_memory))
    targets = _memory_targets(last_extracted)
    if not targets:
        return candidate
    prior_memory = _memory_before_turn(turns, audits, target_turn)
    for target in targets:
        _restore_memory_record(candidate, target, _find_memory_record(prior_memory, target))
    reapplied = storage._apply_memory_events(candidate, last_extracted, expected_turn)
    if storage._normalise_memory(reapplied) != storage._normalise_memory(live_memory):
        raise LegacyRollbackUnsafe("MEMORY_INVERSE_NOT_EXACT")
    return candidate


def _revert_chronology(live: Any, last_extracted: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(live, list):
        raise LegacyRollbackUnsafe("CHRONOLOGY_NOT_LIST")
    added = last_extracted.get("chronology") if isinstance(last_extracted.get("chronology"), list) else []
    if not added:
        return deepcopy(live)
    if len(live) < len(added) or live[-len(added) :] != added:
        raise LegacyRollbackUnsafe("CHRONOLOGY_SUFFIX_MISMATCH")
    return deepcopy(live[: -len(added)])


def _cards_before_turn(
    source: Dict[str, Any], turns: List[Dict[str, Any]], target_turn: int
) -> List[Dict[str, Any]]:
    cards = _initial_cards(source)
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        number = int(turn.get("turn_number", 0) or 0)
        if number <= 0 or number > target_turn:
            continue
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        cards = storage._apply_character_upserts(cards, extracted)
    return cards


def _revert_cards(
    live_cards: List[Dict[str, Any]],
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    last_extracted: Dict[str, Any],
    target_turn: int,
) -> List[Dict[str, Any]]:
    upserts = last_extracted.get("character_upserts") if isinstance(last_extracted.get("character_upserts"), list) else []
    if not upserts:
        return deepcopy(live_cards)
    affected = {storage._card_id(item) for item in upserts if isinstance(item, dict) and storage._card_id(item)}
    prior_replayed = {storage._card_id(card): card for card in _cards_before_turn(source, turns, target_turn)}
    candidate = [deepcopy(card) for card in live_cards if storage._card_id(card) not in affected]
    for character_id in affected:
        if character_id in prior_replayed:
            candidate.append(deepcopy(prior_replayed[character_id]))
    candidate = storage._normalise_cards(candidate)
    if storage._apply_character_upserts(candidate, last_extracted) != storage._normalise_cards(live_cards):
        raise LegacyRollbackUnsafe("CHARACTER_UPSERT_INVERSE_NOT_EXACT")
    return candidate


def _current_roster(state: Dict[str, Any]) -> List[str]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    raw = current.get("present_characters", [])
    if isinstance(raw, dict):
        raw = list(raw.keys())
    if isinstance(raw, str):
        raw = [raw]
    result = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.append(str(value))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        result.insert(0, str(pov["character_id"]))
    return list(dict.fromkeys(result))


def _revert_state(
    live_state: Dict[str, Any],
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    turns: List[Dict[str, Any]],
    audits: List[Dict[str, Any]],
    last_extracted: Dict[str, Any],
    target_turn: int,
    expected_turn: int,
    candidate_memory: Dict[str, Any],
    candidate_chronology: List[Dict[str, Any]],
) -> Dict[str, Any]:
    candidate = deepcopy(live_state)
    last_patch = last_extracted.get("state_patch") if isinstance(last_extracted.get("state_patch"), dict) else {}

    # Relationship snapshots are exact-overwrite blocks. Recover the most recent
    # saved full value before the bad turn, never deep-merge backwards.
    for key in _RELATIONSHIP_KEYS:
        if key not in last_patch:
            continue
        found, prior = _latest_prior_state_value(source, turns, audits, target_turn, (key,))
        if found:
            candidate[key] = prior
        elif key == "relationships":
            candidate[key] = {}
        else:
            candidate.pop(key, None)

    for path in _leaf_paths(last_patch):
        if path[0] in _RELATIONSHIP_KEYS:
            continue
        if path[:2] in {("current", key) for key in _HEADER_KEYS}:
            continue
        found, prior = _latest_prior_state_value(source, turns, audits, target_turn, path)
        if found:
            _set_path(candidate, path, prior)
        else:
            _delete_path(candidate, path)

    current = candidate.get("current") if isinstance(candidate.get("current"), dict) else {}
    candidate["current"] = current
    prior_header = _previous_header(source, turns, audits, target_turn)
    for key in _HEADER_KEYS:
        if key in prior_header:
            current[key] = deepcopy(prior_header[key])

    last_event_current = _last_state_event_current_patch(turns, audits, target_turn)
    for key in _TRANSIENT_CURRENT_KEYS:
        if key in last_event_current:
            current[key] = deepcopy(last_event_current[key])
        else:
            current.pop(key, None)

    previous_roster = set(_current_roster(candidate))
    runtime = candidate.get("characters") if isinstance(candidate.get("characters"), dict) else {}
    candidate["characters"] = runtime
    # A character first entering on the bad turn loses prior last_seen/location
    # in the live file. Refuse that legacy case instead of inventing history.
    for character_id, info in runtime.items():
        if not isinstance(info, dict) or character_id in previous_roster:
            continue
        try:
            seen_turn = int(info.get("last_seen_turn", 0) or 0)
        except (TypeError, ValueError):
            seen_turn = 0
        if seen_turn == expected_turn:
            raise LegacyRollbackUnsafe(f"ENTERED_CHARACTER_NEEDS_OLDER_RUNTIME:{character_id}")

    candidate = storage._refresh_runtime_presence(candidate, cards, target_turn)

    # Familiarity is derived after every successful commit. Rebuild it from the
    # candidate pre-turn memory/chronology/turns, so facts learned only on the bad
    # turn do not leak backwards.
    runtime = candidate.get("characters") if isinstance(candidate.get("characters"), dict) else {}
    for info in runtime.values():
        if isinstance(info, dict):
            info.pop("pov_familiarity", None)
    candidate = refresh_pov_familiarity(
        cards,
        candidate,
        candidate_memory,
        candidate_chronology,
        [turn for turn in turns if int(turn.get("turn_number", 0) or 0) <= target_turn],
        target_turn,
    )
    return candidate


def _forward_saved_turn(
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: List[Dict[str, Any]],
    turns_before: List[Dict[str, Any]],
    last_turn: Dict[str, Any],
) -> Dict[str, Any]:
    turn_number = int(last_turn.get("turn_number", 0) or 0)
    extracted = last_turn.get("extracted") if isinstance(last_turn.get("extracted"), dict) else {}
    after_cards = storage._apply_character_upserts(cards, extracted)
    after_state = _merge_state_patch_exact_relationships(state, extracted.get("state_patch"))
    header = _scene_header_current(str(last_turn.get("scene_output") or ""))
    if header:
        current = after_state.get("current") if isinstance(after_state.get("current"), dict) else {}
        after_state["current"] = storage._deep_merge(current, header)
    after_state = _clean_scene_pointer(after_state, extracted)
    from .game_day import sync_game_day

    after_state = sync_game_day(after_state, source)
    after_state = storage._refresh_runtime_presence(after_state, after_cards, turn_number)
    after_memory = storage._apply_memory_events(storage._normalise_memory(memory), extracted, turn_number)
    after_chronology = deepcopy(chronology)
    if isinstance(extracted.get("chronology"), list):
        after_chronology.extend(deepcopy(extracted["chronology"]))
    after_state = turn_rollback._refresh_derived_state(
        source,
        after_cards,
        after_state,
        after_memory,
        after_chronology,
        [*turns_before, deepcopy(last_turn)],
        turn_number,
    )
    return {
        "characters": after_cards,
        "state": after_state,
        "memory": storage._normalise_memory(after_memory),
        "chronology": after_chronology,
    }


def _matching_snapshot(root, expected_turn: int) -> bool:
    snapshot = storage._read_json(root / SNAPSHOT_FILE, {})
    if not isinstance(snapshot, dict):
        return False
    try:
        return (
            int(snapshot.get("committed_turn", 0) or 0) == expected_turn
            and int(snapshot.get("previous_turn", -1)) == expected_turn - 1
        )
    except (TypeError, ValueError):
        return False


def _prepare_adjacent_snapshot(session_id: str, expected_turn: int) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        if int(meta.get("turn_number", 0) or 0) != expected_turn:
            raise turn_rollback.RollbackError("ROLLBACK_EXPECTED_TURN_MISMATCH")
        turns = storage._read_turns(root)
        if not turns or int(turns[-1].get("turn_number", 0) or 0) != expected_turn:
            raise turn_rollback.RollbackError("ROLLBACK_LAST_TURN_NOT_FOUND")
        target_turn = expected_turn - 1
        last_turn = turns[-1]
        turns_before = turns[:-1]
        last_extracted = last_turn.get("extracted") if isinstance(last_turn.get("extracted"), dict) else {}
        source = storage._read_json(root / "source.json", {})
        audits = storage._read_json(root / "audits.json", [])
        if not isinstance(audits, list):
            audits = []
        live_cards = storage._load_cards(root, source)
        live_state = storage._read_json(root / "state.json", {})
        live_memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        live_chronology = storage._read_json(root / "chronology.json", [])

        candidate_cards = _revert_cards(live_cards, source, turns_before, last_extracted, target_turn)
        candidate_memory = _revert_memory(
            live_memory, last_extracted, turns_before, audits, target_turn, expected_turn
        )
        candidate_chronology = _revert_chronology(live_chronology, last_extracted)
        candidate_state = _revert_state(
            live_state,
            source,
            candidate_cards,
            turns_before,
            audits,
            last_extracted,
            target_turn,
            expected_turn,
            candidate_memory,
            candidate_chronology,
        )

        forward = _forward_saved_turn(
            source,
            candidate_cards,
            candidate_state,
            candidate_memory,
            candidate_chronology,
            turns_before,
            last_turn,
        )
        mismatches = [
            key
            for key, live in (
                ("characters", storage._normalise_cards(live_cards)),
                ("state", live_state),
                ("memory", live_memory),
                ("chronology", live_chronology),
            )
            if forward[key] != live
        ]
        if mismatches:
            raise LegacyRollbackUnsafe("FORWARD_VERIFICATION_MISMATCH:" + ",".join(mismatches))

        snapshot = build_pre_turn_snapshot(root, expected_turn)
        snapshot.update(
            {
                "version": SNAPSHOT_VERSION,
                "snapshot_kind": "verified_adjacent_legacy_recovery",
                "committed_turn": expected_turn,
                "previous_turn": target_turn,
                "meta": deepcopy(meta),
                "characters": candidate_cards,
                "state": candidate_state,
                "memory": candidate_memory,
                "chronology": candidate_chronology,
                "audits": deepcopy(audits),
                "legacy_verification": {
                    "forward_exact": True,
                    "used_full_history_replay": False,
                },
            }
        )
        snapshot["meta"]["turn_number"] = target_turn
        last_audit = max(
            (
                int(audit.get("end_turn", 0) or 0)
                for audit in audits
                if isinstance(audit, dict) and int(audit.get("end_turn", 0) or 0) <= target_turn
            ),
            default=0,
        )
        snapshot["meta"]["last_audit_turn"] = last_audit
        snapshot["meta"]["audit_required"] = bool(
            target_turn > 0 and target_turn % 15 == 0 and last_audit < target_turn
        )
        snapshot["meta"]["handoff_required"] = False
        storage._write_json(root / SNAPSHOT_FILE, snapshot)
        return snapshot


def _rollback_last_turn(session_id: str, expected_turn_number: int, confirm: bool) -> Dict[str, Any]:
    if confirm is not True:
        raise turn_rollback.RollbackError("ROLLBACK_CONFIRMATION_REQUIRED")
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    expected_turn = int(expected_turn_number)
    if not _matching_snapshot(root, expected_turn):
        try:
            _prepare_adjacent_snapshot(session_id, expected_turn)
        except LegacyRollbackUnsafe as exc:
            raise turn_rollback.RollbackError("ROLLBACK_LEGACY_UNSAFE:" + str(exc)) from exc
    result = dict(_ORIGINAL_ROLLBACK(session_id, expected_turn, True))
    if result.get("method") == "exact_pre_turn_snapshot":
        result["historical_replay_used"] = False
    return result


def install() -> None:
    global _ORIGINAL_ROLLBACK
    if _ORIGINAL_ROLLBACK is not None:
        return
    _ORIGINAL_ROLLBACK = turn_rollback.rollback_last_turn
    turn_rollback.rollback_last_turn = _rollback_last_turn
