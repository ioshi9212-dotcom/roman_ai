from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from . import runtime_fixes as base, storage


_ALLOWED_ACTIONS = {"enter", "leave", "move"}
_POSITION_FIELDS = ("zone", "position", "distance_to_pov", "note")


def _resolve_character_id(cards, value: Any) -> str | None:
    return base._resolve_character_id(cards, value)


def _present_ids(cards, state: Dict[str, Any]) -> List[str]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    raw = current.get("present_characters", [])
    values = list(raw.keys()) if isinstance(raw, dict) else [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    result: List[str] = []
    for value in values:
        resolved = _resolve_character_id(cards, value)
        if resolved:
            result.append(resolved)
        elif isinstance(value, dict):
            raw_value = value.get("character_id") or value.get("id") or value.get("name")
            if raw_value:
                result.append(str(raw_value))
        elif value:
            result.append(str(value))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = _resolve_character_id(cards, pov.get("character_id")) or str(pov.get("character_id") or "")
    if pov_id:
        result.insert(0, pov_id)
    return list(dict.fromkeys(result))


def _remote_ids(cards, state: Dict[str, Any]) -> List[str]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    raw = current.get("remote_characters", [])
    values = list(raw.keys()) if isinstance(raw, dict) else [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    result: List[str] = []
    for value in values:
        resolved = _resolve_character_id(cards, value)
        if resolved:
            result.append(resolved)
        elif isinstance(value, dict):
            raw_value = value.get("character_id") or value.get("id") or value.get("name")
            if raw_value:
                result.append(str(raw_value))
        elif value:
            result.append(str(value))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = _resolve_character_id(cards, pov.get("character_id")) or str(pov.get("character_id") or "")
    physical = set(_present_ids(cards, state))
    return [cid for cid in dict.fromkeys(result) if cid and cid != pov_id and cid not in physical]


def _card_name(cards, character_id: str) -> str:
    for card in cards:
        cid = storage._card_id(card)
        if cid != character_id:
            continue
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        return str(card.get("name") or card.get("full_name") or identity.get("name") or character_id)
    return character_id


def _normalise_updates(
    cards,
    raw_updates: Any,
    *,
    allowed_unknown_leave_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    if raw_updates in (None, []):
        return []
    if not isinstance(raw_updates, list):
        base._http_error(409, "PRESENCE_UPDATES_INVALID", "presence_updates must be an array.")
    result: List[Dict[str, Any]] = []
    seen_leave = set()
    for raw in raw_updates:
        if not isinstance(raw, dict):
            base._http_error(409, "PRESENCE_UPDATES_INVALID", "Each presence update must be an object.")
        action = str(raw.get("action") or "").casefold().strip()
        if action not in _ALLOWED_ACTIONS:
            base._http_error(409, "PRESENCE_UPDATES_INVALID", "Presence action must be enter, leave or move.")
        raw_character_id = raw.get("character_id")
        character_id = _resolve_character_id(cards, raw_character_id)
        if not character_id and action == "leave":
            candidate = str(raw_character_id or "").strip()
            if candidate and candidate in (allowed_unknown_leave_ids or set()):
                character_id = candidate
        if not character_id:
            base._http_error(409, "PRESENCE_UPDATES_INVALID", "Unknown character_id in presence_updates.")
        item: Dict[str, Any] = {"character_id": str(character_id), "action": action}
        for field in _POSITION_FIELDS:
            value = raw.get(field)
            if value not in (None, "", [], {}):
                item[field] = deepcopy(value)
        if action == "leave":
            if character_id in seen_leave:
                continue
            seen_leave.add(character_id)
        result.append(item)
    return result


def _raw_roster_values(value: Any) -> List[str]:
    values = list(value.keys()) if isinstance(value, dict) else [value] if isinstance(value, str) else value if isinstance(value, list) else []
    result: List[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("character_id") or item.get("id") or item.get("name")
        if item not in (None, ""):
            result.append(str(item))
    return list(dict.fromkeys(result))


def _apply_presence_contract(payload: Dict[str, Any], *, root) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else None
    if extracted is None:
        return result

    source = storage._read_json(root / "source.json", {})
    old_cards = storage._load_cards(root, source)
    cards = storage._apply_character_upserts(old_cards, extracted)
    state_before = storage._read_json(root / "state.json", {})
    start_roster = _present_ids(cards, state_before)
    start_set = set(start_roster)

    pov = state_before.get("pov") if isinstance(state_before.get("pov"), dict) else {}
    pov_id = _resolve_character_id(cards, pov.get("character_id")) or str(pov.get("character_id") or "")

    raw_state_patch = extracted.get("state_patch")
    if raw_state_patch is not None and not isinstance(raw_state_patch, dict):
        return result
    state_patch = deepcopy(raw_state_patch) if isinstance(raw_state_patch, dict) else {}
    if "current" in state_patch and not isinstance(state_patch.get("current"), dict):
        return result
    current_patch = deepcopy(state_patch.get("current")) if isinstance(state_patch.get("current"), dict) else {}

    direct_supplied = "present_characters" in current_patch
    raw_direct = current_patch.get("present_characters")

    remote_supplied = "remote_characters" in current_patch
    remote_before = _remote_ids(cards, state_before)
    raw_remote = current_patch.get("remote_characters")
    remote_ids: List[str] = []
    if remote_supplied:
        remote_state = {"current": {"remote_characters": raw_remote}, "pov": state_before.get("pov", {})}
        remote_ids = _remote_ids(cards, remote_state)
    else:
        remote_ids = list(remote_before)
    if direct_supplied and raw_direct in (None, "", [], {}):
        base._http_error(
            409,
            "CURRENT_STATE_PATCH_INVALID",
            "current.present_characters cannot be cleared. Use presence_updates with explicit leave actions for real exits.",
        )

    direct_ids: List[str] = []
    if direct_supplied:
        direct_state = {"current": {"present_characters": raw_direct}, "pov": {}}
        direct_ids = _present_ids(cards, direct_state)

    updates = _normalise_updates(
        cards,
        extracted.get("presence_updates"),
        allowed_unknown_leave_ids=start_set,
    )
    entered: List[str] = [cid for cid in direct_ids if cid not in start_set]
    left: List[str] = []
    final = list(start_roster)
    final_set = set(final)

    current_before = state_before.get("current") if isinstance(state_before.get("current"), dict) else {}
    raw_present_before = _raw_roster_values(current_before.get("present_characters"))
    raw_remote_before = _raw_roster_values(current_before.get("remote_characters"))
    positions_before = deepcopy(current_before.get("positions")) if isinstance(current_before.get("positions"), dict) else {}
    positions = deepcopy(positions_before)

    for character_id in entered:
        if character_id not in final_set:
            final.append(character_id)
            final_set.add(character_id)

    for update in updates:
        character_id = update["character_id"]
        action = update["action"]
        if action == "enter":
            if character_id not in final_set:
                final.append(character_id)
                final_set.add(character_id)
            if character_id not in start_set and character_id not in entered:
                entered.append(character_id)
        elif action == "leave":
            if character_id == pov_id:
                base._http_error(409, "POV_LEAVE_INVALID", "POV cannot be removed from the current scene roster.")
            if character_id in final_set:
                final = [cid for cid in final if cid != character_id]
                final_set.discard(character_id)
            if character_id not in left:
                left.append(character_id)
            positions.pop(character_id, None)
        elif action == "move":
            if character_id not in final_set:
                base._http_error(409, "PRESENCE_MOVE_ABSENT", f"Cannot move absent character {_card_name(cards, character_id)} without enter.")

        position_patch = {field: deepcopy(update[field]) for field in _POSITION_FIELDS if field in update}
        if position_patch and action != "leave":
            previous = positions.get(character_id) if isinstance(positions.get(character_id), dict) else {}
            merged = deepcopy(previous)
            merged.update(position_patch)
            positions[character_id] = merged

    if pov_id and pov_id not in final_set:
        final.insert(0, pov_id)
        final_set.add(pov_id)
    final = list(dict.fromkeys(final))

    # A character is either physically present or participating remotely, never both.
    remote_ids = [
        cid for cid in dict.fromkeys(remote_ids)
        if cid and cid != pov_id and cid not in final_set
    ]

    roster_canonicalized = raw_present_before != final
    transition_supplied = bool(updates) or direct_supplied
    if transition_supplied or roster_canonicalized:
        current_patch["present_characters"] = final
        if transition_supplied:
            current_patch["entered_characters"] = entered
            current_patch["left_characters"] = left
    if positions != positions_before:
        current_patch["positions"] = positions
    if remote_supplied or remote_ids != remote_before or raw_remote_before != remote_ids:
        current_patch["remote_characters"] = remote_ids
    if current_patch:
        state_patch["current"] = current_patch
    extracted["state_patch"] = state_patch
    extracted["presence_updates"] = updates
    result["extracted"] = extracted
    return result
