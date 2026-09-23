from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import scene_presence_runtime, session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 11
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


def _core_cast_map(source: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    source = source if isinstance(source, dict) else {}
    novel = source.get("novel") if isinstance(source.get("novel"), dict) else {}
    rows = novel.get("core_cast") if isinstance(novel.get("core_cast"), list) else []
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or "").strip()
        if cid:
            result[cid] = deepcopy(row)
    return result


def _current_game_day(state: Dict[str, Any]) -> int | None:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    try:
        value = int(current.get("game_day", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _post_turn_game_day(state: Dict[str, Any], extracted: Dict[str, Any]) -> int | None:
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current_patch = patch.get("current") if isinstance(patch.get("current"), dict) else {}
    try:
        value = int(current_patch.get("game_day", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else _current_game_day(state)


def _has_active_thread(state: Dict[str, Any], character_id: str) -> bool:
    raw = state.get("threads")
    rows = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "active").casefold() in {"resolved", "closed", "done", "abandoned", "cancelled", "canceled"}:
            continue
        participants = row.get("participants") or row.get("character_ids") or []
        if isinstance(participants, str):
            participants = [participants]
        if isinstance(participants, list) and character_id in {str(value) for value in participants}:
            return True
        for key in ("character_id", "owner_character_id", "target_character_id"):
            if str(row.get(key) or "") == character_id:
                return True
    return False


def _importance_for(
    character_id: str,
    *,
    source_character_ids: set[str],
    core_cast: Dict[str, Dict[str, Any]],
    card: Dict[str, Any],
) -> str:
    if character_id in core_cast:
        return "core"
    if character_id in source_character_ids:
        # Legacy novels predate core_cast. Treat their authored cast as core so old
        # sessions gain anti-forgetting behavior without rewriting canon.
        return "support" if core_cast else "core"
    raw = str(card.get("cast_importance") or "").casefold().strip()
    return raw if raw in {"core", "recurring", "support"} else "recurring"


def _story_function_for(
    character_id: str,
    *,
    core_cast: Dict[str, Dict[str, Any]],
    card: Dict[str, Any],
) -> str:
    core = core_cast.get(character_id) if isinstance(core_cast.get(character_id), dict) else {}
    text = core.get("story_function") or card.get("story_function")
    return " ".join(str(text or "").split())[:360]


def _last_activity_day(row: Dict[str, Any]) -> int | None:
    values: List[int] = []
    for key in ("last_appearance_game_day", "last_contact_game_day", "first_registered_game_day"):
        try:
            value = int(row.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            values.append(value)
    return max(values) if values else None


def _registry_index(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    rank = {"core": 0, "recurring": 1, "support": 2}
    rows: List[Dict[str, Any]] = []
    for cid, raw in registry.items():
        if not isinstance(raw, dict):
            continue
        row = {
            key: deepcopy(raw.get(key))
            for key in (
                "character_id", "name", "card_ref", "origin", "importance", "story_function",
                "status", "first_registered_turn", "first_registered_game_day",
                "last_appearance_turn", "last_appearance_game_day",
                "last_contact_turn", "last_contact_game_day",
                "appearance_count", "last_meaningful_turn", "last_meaningful_event",
            )
            if raw.get(key) not in (None, "", [], {})
        }
        row.setdefault("character_id", cid)
        rows.append(row)
    rows.sort(key=lambda row: (
        rank.get(str(row.get("importance") or "support"), 9),
        0 if row.get("origin") == "player_created" else 1,
        str(row.get("name") or row.get("character_id") or "").casefold(),
    ))
    return rows


def _validate_character_upserts(
    current_cards: List[Dict[str, Any]],
    extracted: Dict[str, Any],
    source_character_ids: set[str],
) -> None:
    existing = {storage._card_id(card): card for card in current_cards}
    rows = extracted.get("character_upserts", []) if isinstance(extracted.get("character_upserts"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = storage._card_id(row)
        if not cid or cid in source_character_ids:
            continue
        prior = existing.get(cid) if isinstance(existing.get(cid), dict) else {}
        story_function = row.get("story_function") or prior.get("story_function")
        if not str(story_function or "").strip():
            raise RuntimeError("CAST_STORY_FUNCTION_REQUIRED")


def _ensure_registry(
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    current_turn: int,
    *,
    source_character_ids: set[str] | None = None,
    source: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    registry = deepcopy(world.get("cast_registry") if isinstance(world.get("cast_registry"), dict) else {})
    runtime = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    source_character_ids = source_character_ids if source_character_ids is not None else {storage._card_id(card) for card in cards}
    core_cast = _core_cast_map(source)
    current_day = _current_game_day(state)
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        info = runtime.get(cid) if isinstance(runtime.get(cid), dict) else {}
        existed = isinstance(registry.get(cid), dict)
        row = deepcopy(registry.get(cid) if existed else {})
        row.setdefault("character_id", cid)
        row["card_ref"] = cid
        row["name"] = storage._card_name(card) or row.get("name") or cid
        row.setdefault("role", storage._card_role(card))
        if not existed:
            row["origin"] = "player_created" if cid in source_character_ids else "story_created"
            row["first_registered_turn"] = 0 if cid in source_character_ids else max(0, current_turn)
            if cid in source_character_ids:
                row["first_registered_game_day"] = 1
            elif current_day:
                row["first_registered_game_day"] = current_day
        else:
            row.setdefault("origin", "player_created" if cid in source_character_ids else "story_created")
            row.setdefault("first_registered_turn", 0 if cid in source_character_ids else max(0, current_turn))
            if cid in source_character_ids:
                row.setdefault("first_registered_game_day", 1)
        row["importance"] = _importance_for(
            cid,
            source_character_ids=source_character_ids,
            core_cast=core_cast,
            card=card,
        )
        story_function = _story_function_for(cid, core_cast=core_cast, card=card)
        if story_function:
            row["story_function"] = story_function
        row.setdefault("appearance_count", 0)
        row["status"] = info.get("status") or card.get("status") or row.get("status") or "active"
        registry[cid] = row
    return registry


def _last_activity_turn(row: Dict[str, Any]) -> int:
    values = []
    for key in ("last_appearance_turn", "last_contact_turn", "first_registered_turn"):
        try:
            values.append(int(row.get(key, 0) or 0))
        except (TypeError, ValueError):
            pass
    return max(values or [0])


def _turn_participant_ids(extracted: Dict[str, Any]) -> set[str]:
    result: set[str] = set()
    scalar_keys = (
        "character_id", "owner_character_id", "speaker", "listener",
        "said_by", "heard_by", "asked_by", "asked_to",
    )
    list_keys = ("participants", "participant_ids")
    for field in ("dialogue_memory_add", "presence_updates"):
        rows = extracted.get(field, []) if isinstance(extracted.get(field), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in scalar_keys:
                value = row.get(key)
                if value not in (None, ""):
                    result.add(str(value))
            for key in list_keys:
                values = row.get(key)
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    result.update(str(value) for value in values if value not in (None, ""))
    return result


def _rotation_pressure(
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    current_turn: int,
    *,
    source_character_ids: set[str] | None = None,
    source: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    registry = _ensure_registry(
        state,
        cards,
        current_turn,
        source_character_ids=source_character_ids,
        source=source,
    )
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(storage._present_character_ids(state))
    current_day = _current_game_day(state)
    thresholds = {
        "core": (18, 2),
        "recurring": (36, 4),
        "support": (60, 7),
    }
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

        last_day = _last_activity_day(row)
        inactive_days = max(0, current_day - last_day) if current_day and last_day else None
        last_appearance_day = row.get("last_appearance_game_day") or row.get("first_registered_game_day")
        try:
            last_appearance_day_int = int(last_appearance_day or 0)
        except (TypeError, ValueError):
            last_appearance_day_int = 0
        since_appearance_days = (
            max(0, current_day - last_appearance_day_int)
            if current_day and last_appearance_day_int
            else None
        )

        origin = str(row.get("origin") or "story_created")
        importance = str(row.get("importance") or ("core" if origin == "player_created" else "recurring"))
        turn_threshold, day_threshold = thresholds.get(importance, thresholds["support"])
        relation = _relation_strength(state, cid)
        has_intent = _has_open_intent(state, cid)
        has_thread = _has_active_thread(state, cid)
        appearance_count = int(row.get("appearance_count", 0) or 0)

        turn_due = inactive_for >= turn_threshold
        day_due = inactive_days is not None and inactive_days >= day_threshold
        relationship_due = relation >= 0.6 and inactive_for >= 5
        forgotten_core = (
            importance == "core"
            and appearance_count <= 1
            and (
                since_appearance >= max(30, turn_threshold)
                or (since_appearance_days is not None and since_appearance_days >= 3)
            )
        )
        due = turn_due or day_due or has_intent or has_thread or relationship_due or forgotten_core
        if not due:
            continue

        turn_pressure = inactive_for / max(1, turn_threshold)
        day_pressure = (inactive_days / max(1, day_threshold)) if inactive_days is not None else 0.0
        importance_bonus = {"core": 45.0, "recurring": 20.0, "support": 5.0}.get(importance, 5.0)
        score = (
            max(turn_pressure, day_pressure) * 25.0
            + importance_bonus
            + relation * 25.0
            + (35.0 if has_intent else 0.0)
            + (30.0 if has_thread else 0.0)
            + (55.0 if forgotten_core else 0.0)
        )
        item: Dict[str, Any] = {
            "character_id": cid,
            "name": row.get("name"),
            "origin": origin,
            "importance": importance,
            "story_function": row.get("story_function"),
            "turns_since_activity": inactive_for,
            "turns_since_appearance": since_appearance,
            "appearance_count": appearance_count,
            "return_rule": "Ищи ближайшую естественную причинную возможность вернуть персонажа; не телепортируй его ради ротации.",
        }
        if inactive_days is not None:
            item["game_days_since_activity"] = inactive_days
        if since_appearance_days is not None:
            item["game_days_since_appearance"] = since_appearance_days
        if forgotten_core:
            item["forgotten_core"] = True
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
        if has_thread:
            item["active_story_thread"] = True
        summary = row.get("last_meaningful_event")
        if summary:
            item["last_meaningful_event"] = str(summary)[:160]
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in scored[:10]]


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
        _validate_character_upserts(current_cards, extracted, source_ids)
        resulting_cards = storage._apply_character_upserts(current_cards, extracted)
        upsert_ids = {storage._card_id(row) for row in extracted.get("character_upserts", []) if isinstance(row, dict) and storage._card_id(row)}
        registry = _ensure_registry(
            state,
            resulting_cards,
            int(meta.get("turn_number", 0) or 0),
            source_character_ids=source_ids,
            source=source,
        )
        post_present = _post_turn_present(state, extracted)
        turn_participants = _turn_participant_ids(extracted)
        chronology = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
        card_map = {storage._card_id(card): card for card in resulting_cards}
        game_day = _post_turn_game_day(state, extracted)

        for cid, row in registry.items():
            if cid in source_ids:
                row["origin"] = "player_created"
                row["first_registered_turn"] = 0
                row.setdefault("first_registered_game_day", 1)
            elif cid in upsert_ids or row.get("origin") != "story_created":
                row["origin"] = "story_created"
                if not int(row.get("first_registered_turn", 0) or 0):
                    row["first_registered_turn"] = turn_number
                if game_day and not int(row.get("first_registered_game_day", 0) or 0):
                    row["first_registered_game_day"] = game_day

            if cid in post_present:
                row["last_appearance_turn"] = turn_number
                row["last_contact_turn"] = turn_number
                if game_day:
                    row["last_appearance_game_day"] = game_day
                    row["last_contact_game_day"] = game_day
                if int(row.get("_seen_this_turn", 0) or 0) != turn_number:
                    row["appearance_count"] = int(row.get("appearance_count", 0) or 0) + 1
                    row["_seen_this_turn"] = turn_number

            summary = _event_summary_for(cid, card_map.get(cid, {}), chronology)
            if summary:
                row["last_meaningful_event"] = summary
                row["last_meaningful_turn"] = turn_number
            if cid in turn_participants:
                row["last_contact_turn"] = turn_number
                if game_day:
                    row["last_contact_game_day"] = game_day

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
        registry = _ensure_registry(
            state, cards, current_turn, source_character_ids=source_ids, source=source
        )
        pressure = _rotation_pressure(
            state, cards, current_turn, source_character_ids=source_ids, source=source
        )
        active_rows = [row for row in registry.values() if isinstance(row, dict) and not _is_inactive(row.get("status"))]
        context["cast_registry"] = {
            "version": _VERSION,
            "persistent": True,
            "authoritative_live_roster": True,
            "registry_index": _registry_index(registry),
            "active_count": len(active_rows),
            "player_created_active_count": sum(1 for row in active_rows if row.get("origin") == "player_created"),
            "story_created_active_count": sum(1 for row in active_rows if row.get("origin") == "story_created"),
            "core_active_count": sum(1 for row in active_rows if row.get("importance") == "core"),
            "rotation_pressure": pressure,
            "mandatory_rotation_consideration": bool(pressure),
            "instruction": (
                "registry_index = полный компактный каталог всех зарегистрированных персонажей. "
                "character_id/card_ref ведут к полной карточке. Перед сценой проверь весь каталог, особенно core. "
                "rotation_pressure не означает телепортацию: ищи ближайшую естественную причинную возможность вернуть персонажа. "
                "Новый именованный NPC может быть одноразовым extra без карточки; если он стал другом, врагом, конкурентом, "
                "постоянным коллегой, романтической/сюжетной линией или иной устойчивой фигурой, сохрани character_upsert "
                "с короткой режиссёрской story_function."
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
