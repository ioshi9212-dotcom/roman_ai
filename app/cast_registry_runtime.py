from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 1
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
        rows = rows.values()
    for row in rows if isinstance(rows, (list, tuple, set)) else []:
        if isinstance(row, dict) and str(row.get("status") or "active").casefold() not in {"resolved", "closed", "done", "abandoned", "cancelled", "canceled"}:
            return True
    return False


def _ensure_registry(state: Dict[str, Any], cards: List[Dict[str, Any]], current_turn: int) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    registry = world.get("cast_registry") if isinstance(world.get("cast_registry"), dict) else {}
    registry = deepcopy(registry)
    runtime = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    present = set(storage._present_character_ids(state))
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        info = runtime.get(cid) if isinstance(runtime.get(cid), dict) else {}
        row = registry.get(cid) if isinstance(registry.get(cid), dict) else {}
        row = deepcopy(row)
        row.setdefault("character_id", cid)
        row.setdefault("name", storage._card_name(card))
        row.setdefault("role", storage._card_role(card))
        row.setdefault("origin", "player_created")
        row.setdefault("first_registered_turn", 0)
        row["status"] = info.get("status") or card.get("status") or row.get("status") or "active"
        if cid in present:
            row["last_appearance_turn"] = current_turn
            row["last_contact_turn"] = current_turn
            row["appearance_count"] = int(row.get("appearance_count", 0) or 0) + (0 if row.get("_seen_this_turn") == current_turn else 1)
            row["_seen_this_turn"] = current_turn
        registry[cid] = row
    world = deepcopy(world)
    world["cast_registry"] = registry
    state["world"] = world
    return registry


def _rotation_pressure(state: Dict[str, Any], cards: List[Dict[str, Any]], current_turn: int) -> List[Dict[str, Any]]:
    registry = _ensure_registry(state, cards, current_turn)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(storage._present_character_ids(state))
    scored: List[tuple[float, Dict[str, Any]]] = []
    for cid, row in registry.items():
        if cid == pov_id or cid in present or not isinstance(row, dict) or _is_inactive(row.get("status")):
            continue
        last = int(row.get("last_appearance_turn", 0) or 0)
        absent = max(0, current_turn - last) if last else current_turn
        origin = str(row.get("origin") or "story_created")
        relation = _relation_strength(state, cid)
        has_intent = _has_open_intent(state, cid)
        player_created = origin == "player_created"
        due = absent >= (8 if player_created else 15) or has_intent or (relation >= 0.6 and absent >= 5)
        if not due:
            continue
        score = float(absent) + (40.0 if player_created else 10.0) + relation * 30.0 + (35.0 if has_intent else 0.0)
        scored.append((score, {
            "character_id": cid,
            "name": row.get("name"),
            "role": row.get("role"),
            "origin": origin,
            "turns_since_appearance": absent,
            "relationship_salience": round(relation, 2),
            "open_intent": has_intent,
            "last_meaningful_event": row.get("last_meaningful_event"),
            "guidance": "This active character is due for consideration. Reintroduce only through a natural causal channel; player-created cast must not disappear merely because current relationship values are low. Load the character bundle before participation.",
        }))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:8]]


def _rewrite_packet(session_id: str, base_result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base_result
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base_result
        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        meta = storage._read_json(root / "meta.json", {})
        current_turn = int(meta.get("turn_number", 0) or 0)
        registry = _ensure_registry(state, cards, current_turn)
        pressure = _rotation_pressure(state, cards, current_turn)
        storage._write_json(root / "state.json", state)
        context["cast_registry"] = {
            "version": _VERSION,
            "characters": [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in registry.values()],
            "rotation_pressure": pressure,
            "rules": [
                "All active player-created characters remain part of the living cast even with weak or undeveloped relationships.",
                "Long absence creates re-entry pressure; strong relationships and open intents increase frequency but are not the only source of relevance.",
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


def _event_summary_for(character_id: str, card: Dict[str, Any], chronology: Any) -> str | None:
    names = [name.casefold() for name in storage._card_names(card) if str(name).strip()]
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


def _update_after_commit(session_id: str, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        turn_number = int(result.get("turn_number") or storage._read_json(root / "meta.json", {}).get("turn_number", 0) or 0)
        registry = _ensure_registry(state, cards, turn_number)
        extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
        upsert_ids = {storage._card_id(row) for row in extracted.get("character_upserts", []) if isinstance(row, dict) and storage._card_id(row)}
        present = set(storage._present_character_ids(state))
        chronology = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
        card_map = {storage._card_id(card): card for card in cards}
        for cid, row in registry.items():
            if cid in upsert_ids and int(row.get("first_registered_turn", 0) or 0) == 0 and row.get("origin") == "player_created" and turn_number > 0:
                row["origin"] = "story_created"
                row["first_registered_turn"] = turn_number
            if cid in present:
                row["last_appearance_turn"] = turn_number
                row["last_contact_turn"] = turn_number
            summary = _event_summary_for(cid, card_map.get(cid, {}), chronology)
            if summary:
                row["last_meaningful_event"] = summary
                row["last_meaningful_turn"] = turn_number
        world = state.get("world") if isinstance(state.get("world"), dict) else {}
        world["cast_registry"] = registry
        state["world"] = world
        storage._write_json(root / "state.json", state)


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(_ORIGINAL_COMMIT(session_id, payload))
    _update_after_commit(session_id, payload, result)
    return result


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
