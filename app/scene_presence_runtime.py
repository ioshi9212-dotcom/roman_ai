from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import runtime_fixes as base, storage


_ORIGINAL_PREPARE_EXTRACTED = base._prepare_extracted_for_commit
_ORIGINAL_REWRITE_TURN_PACKET = base._rewrite_turn_packet

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


def _card_name(cards, character_id: str) -> str:
    for card in cards:
        cid = storage._card_id(card)
        if cid != character_id:
            continue
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        return str(card.get("name") or card.get("full_name") or identity.get("name") or character_id)
    return character_id


def _normalise_updates(cards, raw_updates: Any) -> List[Dict[str, Any]]:
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
        character_id = _resolve_character_id(cards, raw.get("character_id"))
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

    state_patch = deepcopy(extracted.get("state_patch")) if isinstance(extracted.get("state_patch"), dict) else {}
    current_patch = deepcopy(state_patch.get("current")) if isinstance(state_patch.get("current"), dict) else {}

    # Backward compatibility: a directly supplied full roster may add characters, but omission
    # alone can never remove somebody who was already present. Removal requires explicit leave.
    raw_direct = current_patch.get("present_characters")
    direct_ids: List[str] = []
    if raw_direct not in (None, "", [], {}):
        direct_state = {"current": {"present_characters": raw_direct}, "pov": {}}
        direct_ids = _present_ids(cards, direct_state)

    updates = _normalise_updates(cards, extracted.get("presence_updates"))
    entered: List[str] = [cid for cid in direct_ids if cid not in start_set]
    left: List[str] = []
    final = list(start_roster)
    final_set = set(final)

    positions_before = {}
    current_before = state_before.get("current") if isinstance(state_before.get("current"), dict) else {}
    if isinstance(current_before.get("positions"), dict):
        positions_before = deepcopy(current_before["positions"])
    positions = positions_before

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
            # Moving within the current scene never removes the character from the roster.
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

    current_patch["present_characters"] = final
    current_patch["entered_characters"] = entered
    current_patch["left_characters"] = left
    if positions != positions_before or positions:
        current_patch["positions"] = positions
    state_patch["current"] = current_patch
    extracted["state_patch"] = state_patch
    extracted["presence_updates"] = updates
    result["extracted"] = extracted
    return result


def _prepare_extracted_for_commit(*args, **kwargs):
    if not args:
        return _ORIGINAL_PREPARE_EXTRACTED(*args, **kwargs)
    root = kwargs.get("root")
    if root is None:
        return _ORIGINAL_PREPARE_EXTRACTED(*args, **kwargs)
    payload = _apply_presence_contract(args[0], root=root)
    return _ORIGINAL_PREPARE_EXTRACTED(payload, *args[1:], **kwargs)


def _rewrite_turn_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    result = _ORIGINAL_REWRITE_TURN_PACKET(session_id, manifest)
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return result
    context = json.loads(raw)

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = context.get("scene_state") if isinstance(context.get("scene_state"), dict) else storage._read_json(root / "state.json", {})
    start_roster = _present_ids(cards, state)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = _resolve_character_id(cards, pov.get("character_id")) or str(pov.get("character_id") or "")

    roster = []
    for character_id in start_roster:
        roster.append(
            {
                "character_id": character_id,
                "name": _card_name(cards, character_id),
                "full_card_path": f"all_character_cards[{character_id}]",
                "memory_path": f"memory_full.characters[{character_id}]",
                "relationship_path": f"relationship_lens owner_character_id={character_id}",
            }
        )

    context["scene_focus"] = {
        "pov_character_id": pov_id or None,
        "present_character_ids": start_roster,
        "required_full_character_ids": start_roster,
        "instruction": (
            "Every character in required_full_character_ids remains physically active in the scene until an explicit leave transition is committed. "
            "Read each full card/memory/relationship and evaluate perception/reaction before writing important beats."
        ),
    }
    context["scene_presence"] = {
        "start_present_character_ids": start_roster,
        "roster": roster,
        "final_roster_formula": "start roster + enter - leave; move does not change membership",
        "pov_must_remain_present": True,
        "direct_roster_omission_cannot_remove": True,
        "presence_updates": {
            "field": "extracted.presence_updates",
            "actions": ["enter", "leave", "move"],
            "examples": [
                {"character_id": "npc_id", "action": "enter", "zone": "doorway"},
                {"character_id": "npc_id", "action": "move", "zone": "window", "note": "a few steps from POV"},
                {"character_id": "npc_id", "action": "leave"},
            ],
        },
        "instruction": (
            "STRUCTURAL PRESENCE CONTRACT. Do not rewrite current.present_characters as a free-form snapshot. "
            "If nobody physically enters/leaves/moves, presence_updates may be empty and the start roster persists automatically. "
            "Use enter only for a real arrival, leave only for a real physical exit from the accessible scene, and move for stepping aside, sitting farther away, going to a window or another position inside the same scene. "
            "A silent NPC, a change of dialogue focus or a POV step to the side is NOT leave."
        ),
    }

    persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
    persistence["presence_updates"] = {
        "optional": True,
        "field": "extracted.presence_updates",
        "required_when": "Only when a character physically enters, leaves, or changes position inside the scene.",
        "rule": "Omission preserves the start roster. Only explicit leave removes an already-present character.",
    }
    context["persistence_contract"] = persistence

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i : i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    packet["presence_runtime_version"] = 1
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(result)
    result["chunk_count"] = len(chunks)
    result["instruction"] = (
        str(result.get("instruction", "")).rstrip()
        + " Scene presence is structural: start roster persists; use extracted.presence_updates enter/leave/move for physical transitions."
    ).strip()
    return result


def install() -> None:
    base._prepare_extracted_for_commit = _prepare_extracted_for_commit
    base._rewrite_turn_packet = _rewrite_turn_packet
