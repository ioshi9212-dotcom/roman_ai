from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import scene_presence_runtime, session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 9
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


def _relationship_signals(state: Dict[str, Any], character_id: str) -> Dict[str, float]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    row = relationships.get(character_id) if isinstance(relationships.get(character_id), dict) else {}
    values = {
        str(label): float(value)
        for label, value in row.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) != 0
    }
    ranked = sorted(values.items(), key=lambda item: abs(item[1]), reverse=True)[:4]
    return {label: value for label, value in ranked}


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
    for key in ("last_appearance_turn", "last_contact_turn", "last_meaningful_turn", "first_registered_turn"):
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
        first_registered = int(row.get("first_registered_turn", 0) or 0)
        last_appearance = int(row.get("last_appearance_turn", 0) or 0)
        last_activity = _last_activity_turn(row)
        inactive_for = max(0, current_turn - last_activity) if last_activity else current_turn
        appearance_baseline = last_appearance or first_registered
        since_appearance = max(0, current_turn - appearance_baseline) if appearance_baseline else current_turn
        origin = str(row.get("origin") or "story_created")
        relation = _relation_strength(state, cid)
        has_intent = _has_open_intent(state, cid)
        player_created = origin == "player_created"
        due = inactive_for >= (8 if player_created else 15) or has_intent or (relation >= 0.6 and inactive_for >= 5)
        if not due:
            continue
        score = float(inactive_for) + (40.0 if player_created else 10.0) + relation * 30.0 + (35.0 if has_intent else 0.0)
        item: Dict[str, Any] = {
            "character_id": cid,
            "origin": origin,
            "turns_since_activity": inactive_for,
            "turns_since_appearance": since_appearance,
        }
        if relation:
            item["relationship_salience"] = round(relation, 2)
            signals = _relationship_signals(state, cid)
            if signals:
                item["relationship_signals"] = signals
                item["relationship_behavior_note"] = (
                    "Use these values through this NPC's character. Strong warm/attachment metrics may support contact; "
                    "strong resentment/fear/suspicion may instead support avoidance, testing, confrontation or interference."
                )
        if has_intent:
            item["open_intent"] = True
        summary = row.get("last_meaningful_event")
        if summary:
            item["last_meaningful_event"] = str(summary)[:160]
        scored.append((score, item))
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


def _registry_delta(before: Any, after: Dict[str, Any]) -> Dict[str, Any]:
    before = before if isinstance(before, dict) else {}
    return {
        cid: deepcopy(row)
        for cid, row in after.items()
        if cid not in before or before.get(cid) != row
    }


def _with_registry_patch(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        meta = storage._read_json(root / "meta.json", {})
        turn_number = int(meta.get("turn_number", 0) or 0) + 1
        current_cards = storage._load_cards(root, source)
        source_ids = {storage._card_id(card) for card in storage._normalise_cards(source.get("characters", []))}
        world_before = state.get("world") if isinstance(state.get("world"), dict) else {}
        registry_before = world_before.get("cast_registry") if isinstance(world_before.get("cast_registry"), dict) else {}

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

        delta = _registry_delta(registry_before, registry)
        if delta:
            state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
            state_patch = deepcopy(state_patch)
            world_patch = state_patch.get("world") if isinstance(state_patch.get("world"), dict) else {}
            world_patch = deepcopy(world_patch)
            existing_registry_patch = world_patch.get("cast_registry") if isinstance(world_patch.get("cast_registry"), dict) else {}
            merged_registry_patch = deepcopy(existing_registry_patch)
            merged_registry_patch.update(delta)
            world_patch["cast_registry"] = merged_registry_patch
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
        active_rows = [row for row in registry.values() if isinstance(row, dict) and not _is_inactive(row.get("status"))]
        context["cast_registry"] = {
            "version": _VERSION,
            "persistent": True,
            "registry_index_path": "character_registry",
            "active_count": len(active_rows),
            "player_created_active_count": sum(1 for row in active_rows if row.get("origin") == "player_created"),
            "story_created_active_count": sum(1 for row in active_rows if row.get("origin") == "story_created"),
            "rotation_pressure": pressure,
            "instruction": (
                "Use character_registry for names/roles and rotation_pressure as anti-forgetting priority. "
                "Player-created cast stays eligible; strong relationships/open intents increase narrative salience. "
                "Relationship type changes HOW initiative appears: contact, avoidance, testing, help, jealousy or conflict must follow "
                "the specific NPC. Re-entry must be causal and offscreen participation requires the character bundle."
            ),
        }

        cast_index = context.get("cast_index")
        if isinstance(cast_index, list):
            context["cast_index"] = [
                {"character_id": str(row.get("character_id"))}
                for row in cast_index
                if isinstance(row, dict) and row.get("character_id")
            ]

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
