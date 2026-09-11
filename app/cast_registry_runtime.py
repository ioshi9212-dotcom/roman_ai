from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import scene_presence_runtime, session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 5
_TERMINAL = {"dead", "deceased", "inactive", "removed", "мертв", "мёртв", "погиб", "умер", "неактив"}


def _status(value: Any) -> str:
    return str(value or "active").casefold().strip()


def _is_inactive(value: Any) -> bool:
    text = _status(value)
    return any(token in text for token in _TERMINAL)


def _relation_strength(state: Dict[str, Any], character_id: str) -> float:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    row = relationships.get(character_id) if isinstance(relationships.get(character_id), dict) else {}
    numbers = [abs(float(value)) for value in row.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if not numbers:
        return 0.0
    maximum = max(numbers)
    return min(1.0, maximum / 10.0 if maximum <= 10 else maximum / 100.0)


def _has_open_intent(state: Dict[str, Any], character_id: str) -> bool:
    intents = state.get("npc_intents")
    rows: Any = []
    if isinstance(intents, dict):
        rows = intents.get(character_id, [])
    elif isinstance(intents, list):
        rows = [row for row in intents if isinstance(row, dict) and str(row.get("character_id") or "") == character_id]
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and str(row.get("status") or "active").casefold() not in {"resolved", "closed", "done", "abandoned", "cancelled", "canceled"}
        for row in rows
    )


def _ensure_registry(
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    current_turn: int,
    *,
    source_character_ids: set[str] | None = None,
) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    registry = deepcopy(world.get("cast_registry") if isinstance(world.get("cast_registry"), dict) else {})
    runtime = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    source_character_ids = source_character_ids if source_character_ids is not None else {storage._card_id(card) for card in cards}
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        info = runtime.get(cid) if isinstance(runtime.get(cid), dict) else {}
        existed = isinstance(registry.get(cid), dict)
        row = deepcopy(registry.get(cid) if existed else {})
        row.setdefault("character_id", cid)
        row.setdefault("name", storage._card_name(card))
        row.setdefault("role", storage._card_role(card))
        if not existed:
            row["origin"] = "player_created" if cid in source_character_ids else "story_created"
            row["first_registered_turn"] = 0 if cid in source_character_ids else max(0, current_turn)
        else:
            row.setdefault("origin", "player_created" if cid in source_character_ids else "story_created")
            row.setdefault("first_registered_turn", 0 if cid in source_character_ids else max(0, current_turn))
        row.setdefault("appearance_count", 0)
        row["status"] = info.get("status") or card.get("status") or row.get("status") or "active"
        registry[cid] = row
    return registry


def _last_activity_turn(row: Dict[str, Any]) -> int:
    values = []
    for key in ("last_appearance_turn", "last_contact_turn", "last_meaningful_turn"):
        try:
            values.append(int(row.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
    return max(values or [0])


def _rotation_pressure(
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    current_turn: int,
    *,
    source_character_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    registry = _ensure_registry(state, cards, current_turn, source_character_ids=source_character_ids)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(storage._present_character_ids(state))
    scored: List[tuple[float, Dict[str, Any]]] = []
    for cid, row in registry.items():
        if cid == pov_id or cid in present or not isinstance(row, dict) or _is_inactive(row.get("status")):
            continue
        last_appearance = int(row.get("last_appearance_turn", 0) or 0)
        last_activity = _last_activity_turn(row)
        inactive_for = max(0, current_turn - last_activity) if last_activity else current_turn
        since_appearance = max(0, current_turn - last_appearance) if last_appearance else current_turn
        origin = str(row.get("origin") or "story_created")
        relation = _relation_strength(state, cid)
        has_intent = _has_open_intent(state, cid)
        player_created = origin == "player_created"
        due = inactive_for >= (8 if player_created else 15) or has_intent or (relation >= 0.6 and inactive_for >= 5)
        if not due:
            continue
        score = float(inactive_for) + (40.0 if player_created else 10.0) + relation * 30.0 + (35.0 if has_intent else 0.0)
        scored.append((score, {
            "character_id": cid,
            "name": row.get("name"),
            "role": row.get("role"),
            "origin": origin,
            "turns_since_activity": inactive_for,
            "turns_since_appearance": since_appearance,
            "relationship_salience": round(relation, 2),
            "open_intent": has_intent,
            "last_meaningful_event": row.get("last_meaningful_event"),
            "guidance": "This active character is due for consideration. Reintroduce only through a natural causal channel; player-created cast must not disappear merely because current relationship values are low. Load the character bundle before participation.",
        }))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:8]]


def _post_turn_present(state: Dict[str, Any], extracted: Dict[str, Any]) -> set[str]:
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current_patch = patch.get("current") if isinstance(patch.get("current"), dict) else {}
    normalized = current_patch.get("present_characters")
    if isinstance(normalized, list):
        return {str(value) for value in normalized if value}
    present = set(storage._present_character_ids(state))
    for row in extracted.get("presence_updates", []) if isinstance(extracted.get("presence_updates"), list) else []:
        if not isinstance(row, dict) or not row.get("character_id"):
            continue
        cid = str(row["character_id"])
        action = str(row.get("action") or "").casefold()
        if action == "enter":
            present.add(cid)
        elif action == "leave":
            present.discard(cid)
    return present


def _event_summary_for(character_id: str, card: Dict[str, Any], chronology: Any) -> str | None:
    names = [str(name).casefold() for name in storage._card_names(card) if str(name).strip()]
    for event in reversed(chronology if isinstance(chronology, list) else []):
        if not isinstance(event, dict):
            continue
        ids = event.get("character_ids") or event.get("participants") or event.get("characters") or []
        if isinstance(ids, str):
            ids = [ids]
        serialized = json.dumps(event, ensure_ascii=False).casefold()
        if character_id in [str(value) for value in ids] or any(name in serialized for name in names):
            summary = event.get("summary") or event.get("event") or event.get("text") or event.get("description")
            if summary:
                return " ".join(str(summary).split())[:320]
    return None


def _with_registry_patch(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        meta = storage._read_json(root / "meta.json", {})
        turn_number = int(meta.get("turn_number", 0) or 0) + 1
        current_cards = storage._load_cards(root, source)
        source_ids = {storage._card_id(card) for card in storage._normalise_cards(source.get("characters", []))}

        prepared = scene_presence_runtime._apply_presence_contract(deepcopy(payload), root=root)
        extracted = prepared.get("extracted") if isinstance(prepared.get("extracted"), dict) else {}
        extracted = deepcopy(extracted)
        resulting_cards = storage._apply_character_upserts(current_cards, extracted)
        upsert_ids = {storage._card_id(row) for row in extracted.get("character_upserts", []) if isinstance(row, dict) and storage._card_id(row)}
        registry = _ensure_registry(
            state,
            resulting_cards,
            int(meta.get("turn_number", 0) or 0),
            source_character_ids=source_ids,
        )
        post_present = _post_turn_present(state, extracted)
        chronology = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
        card_map = {storage._card_id(card): card for card in resulting_cards}

        for cid, row in registry.items():
            if cid in source_ids:
                row["origin"] = "player_created"
                row["first_registered_turn"] = 0
            elif cid in upsert_ids or row.get("origin") != "story_created":
                row["origin"] = "story_created"
                if not int(row.get("first_registered_turn", 0) or 0):
                    row["first_registered_turn"] = turn_number

            if cid in post_present:
                row["last_appearance_turn"] = turn_number
                row["last_contact_turn"] = turn_number
                if int(row.get("_seen_this_turn", 0) or 0) != turn_number:
                    row["appearance_count"] = int(row.get("appearance_count", 0) or 0) + 1
                    row["_seen_this_turn"] = turn_number

            summary = _event_summary_for(cid, card_map.get(cid, {}), chronology)
            if summary:
                row["last_meaningful_event"] = summary
                row["last_meaningful_turn"] = turn_number
                row["last_contact_turn"] = turn_number

        state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
        state_patch = deepcopy(state_patch)
        world_patch = state_patch.get("world") if isinstance(state_patch.get("world"), dict) else {}
        world_patch = deepcopy(world_patch)
        world_patch["cast_registry"] = registry
        state_patch["world"] = world_patch
        extracted["state_patch"] = state_patch
        prepared["extracted"] = extracted
        return prepared


def _rewrite_packet(session_id: str, base_result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base_result
        if packet.get("cast_registry_version") == _VERSION:
            return base_result
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base_result
        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        source_ids = {storage._card_id(card) for card in storage._normalise_cards(source.get("characters", []))}
        current_turn = int(storage._read_json(root / "meta.json", {}).get("turn_number", 0) or 0)
        registry = _ensure_registry(state, cards, current_turn, source_character_ids=source_ids)
        pressure = _rotation_pressure(state, cards, current_turn, source_character_ids=source_ids)
        context["cast_registry"] = {
            "version": _VERSION,
            "characters": [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in registry.values()],
            "rotation_pressure": pressure,
            "rules": [
                "All active player-created characters remain part of the living cast even with weak or undeveloped relationships.",
                "Long inactivity creates re-entry pressure; strong relationships and open intents increase frequency but are not the only source of relevance.",
                "Dead/inactive characters stay registered for references and consequences but are excluded from ordinary rotation.",
                "A recurring named story-created NPC should have a role, independent goal/driver and story function before being upserted.",
                "Never inject a due NPC randomly. Use a plausible message, work duty, location, shared contact, consequence, appointment, conflict or other causal channel.",
            ],
        }
        living = context.get("living_world") if isinstance(context.get("living_world"), dict) else {}
        living["cast_rotation_pressure"] = pressure
        context["living_world"] = living
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["cast_registry_version"] = _VERSION
        storage._write_json(root / "turn_packet.json", packet)
        result = dict(base_result)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "cast_registry": True,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(_ORIGINAL_COMMIT(session_id, _with_registry_patch(session_id, payload)))
    result["cast_registry_persisted"] = True
    return result


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
