from __future__ import annotations

from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import storage


FILE_NAME = "relationships.json"
MAX_DIMENSIONS_PER_NPC = 10
ORDINARY_DELTA_LIMIT = 3.0
MAX_RELATION_WORDS = 10
MECHANICS = (
    "NPC→POV: до 10 свободных показателей 1–100; 0 удаляет. "
    "Бери текущие значения только отсюда. Меняй лишь затронутые, обычно на ±1–3; "
    "серьёзное событие допускает большой скачок. Противоречия нормальны; "
    "отношения влияют на речь и действия с учётом характера. "
    "Незнакомым — без показателей; первая встреча даёт впечатление. "
    "NPC→NPC: направленный текст до 10 слов. История здесь не хранится."
)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _bounded_value(value: Any) -> int | float:
    number = max(0.0, min(100.0, float(value)))
    return _number(number)


def _resolve_character_id(cards: Iterable[Dict[str, Any]], raw: Any) -> str | None:
    needle = _norm(raw)
    if not needle:
        return None
    for card in cards:
        cid = storage._card_id(card)
        if _norm(cid) == needle:
            return cid
        for alias in storage._card_names(card):
            if _norm(alias) == needle:
                return cid
    return None


def _relationship_rows(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = card.get("relationships")
    if isinstance(raw, list):
        return [deepcopy(row) for row in raw if isinstance(row, dict)]
    if isinstance(raw, dict):
        if any(key in raw for key in ("target_character_id", "target_id", "target", "with", "character_id")):
            return [deepcopy(raw)]
        rows: List[Dict[str, Any]] = []
        for target, value in raw.items():
            if not isinstance(value, dict):
                continue
            row = deepcopy(value)
            row.setdefault("target_character_id", target)
            rows.append(row)
        return rows
    return []


def _target_id(cards: List[Dict[str, Any]], row: Dict[str, Any]) -> str | None:
    raw = (
        row.get("target_character_id")
        or row.get("target_id")
        or row.get("target")
        or row.get("with")
        or row.get("character_id")
    )
    return _resolve_character_id(cards, raw)


def _qualitative_relation(row: Dict[str, Any]) -> str:
    text = row.get("description") or " ".join(
        str(row.get(key) or "")
        for key in ("relationship_type", "relationship_context", "current_dynamic", "behavioral_pattern")
    )
    return " ".join(str(text).split()[:MAX_RELATION_WORDS])

def _empty_store(pov_id: str) -> Dict[str, Any]:
    return {
        "version": 3,
        "mechanics": MECHANICS,
        "pov_character_id": str(pov_id or ""),
        "npc_to_pov": {},
        "npc_to_npc": {},
    }


def _profile_store(cards: List[Dict[str, Any]], pov_id: str) -> Dict[str, Any]:
    store = _empty_store(pov_id)
    for card in cards:
        owner_id = storage._card_id(card)
        if not owner_id or owner_id == pov_id:
            continue
        for row in _relationship_rows(card):
            target_id = _target_id(cards, row)
            if not target_id or target_id == owner_id:
                continue
            if target_id != pov_id:
                text = _qualitative_relation(row)
                if text:
                    store["npc_to_npc"].setdefault(owner_id, {})[target_id] = text
                continue
            dimensions = store["npc_to_pov"].setdefault(owner_id, {"dimensions": {}})["dimensions"]
            for item in row.get("dimensions", []) if isinstance(row.get("dimensions"), list) else []:
                if not isinstance(item, dict):
                    continue
                label = " ".join(str(item.get("label") or item.get("key") or "").split())
                value = item.get("value")
                if label and _is_number(value) and value > 0:
                    dimensions[label] = {"value": _bounded_value(value)}
    return normalize_store(store, pov_id)

def _migrate_legacy_state(store: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(store)
    for owner_id, raw in _mapping(state.get("relationships")).items():
        if isinstance(raw, dict):
            result["npc_to_pov"][str(owner_id)] = {"dimensions": {
                str(label): {"value": _bounded_value(value)}
                for label, value in raw.items() if _is_number(value) and value > 0
            }}
    for owner_id, targets in _mapping(state.get("npc_relationships")).items():
        if isinstance(targets, dict):
            for target_id, raw in targets.items():
                text = _qualitative_relation(raw) if isinstance(raw, dict) else " ".join(str(raw).split()[:MAX_RELATION_WORDS])
                if text:
                    result["npc_to_npc"].setdefault(str(owner_id), {})[str(target_id)] = text
    return normalize_store(result)

def normalize_store(value: Any, pov_id: str = "") -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = _empty_store(str(source.get("pov_character_id") or pov_id or ""))
    for owner_id, row in _mapping(source.get("npc_to_pov")).items():
        if not isinstance(row, dict) or str(owner_id) == result["pov_character_id"]:
            continue
        clean = {}
        for label, item in _mapping(row.get("dimensions")).items():
            value = item.get("value") if isinstance(item, dict) else item
            name = " ".join(str(label).split())
            if not name or not _is_number(value) or value <= 0:
                continue
            existing = next((key for key in clean if _norm(key) == _norm(name)), None)
            if existing is not None:
                clean[existing] = {"value": _bounded_value(value)}
            elif len(clean) < MAX_DIMENSIONS_PER_NPC:
                clean[name] = {"value": _bounded_value(value)}
        # An empty record remembers an encounter, not a fabricated attitude.
        result["npc_to_pov"][str(owner_id)] = {"dimensions": clean}
    for owner_id, targets in _mapping(source.get("npc_to_npc")).items():
        if not isinstance(targets, dict) or str(owner_id) == result["pov_character_id"]:
            continue
        for target_id, text in targets.items():
            if str(target_id) in {str(owner_id), result["pov_character_id"]}:
                continue
            short = " ".join(str(text or "").split()[:MAX_RELATION_WORDS])
            if short:
                result["npc_to_npc"].setdefault(str(owner_id), {})[str(target_id)] = short
    return result


def build_initial_store(cards: List[Dict[str, Any]], state: Dict[str, Any], pov_id: str) -> Dict[str, Any]:
    return _migrate_legacy_state(_profile_store(cards, pov_id), state)


def load(root: Path, *, cards: List[Dict[str, Any]], state: Dict[str, Any], pov_id: str) -> Dict[str, Any]:
    path = root / FILE_NAME
    if path.exists():
        raw = storage._read_json(path, {})
        store = normalize_store(raw, pov_id)
        if raw != store:
            storage._write_json(path, store)
        return store
    store = build_initial_store(cards, state, pov_id)
    storage._write_json(path, store)
    cleaned_state = deepcopy(state)
    for key in ("relationships", "relationship_documents", "relationship_schemas", "npc_relationships"):
        cleaned_state.pop(key, None)
    if cleaned_state != state:
        storage._write_json(root / "state.json", cleaned_state)
    return store


def character_relation(store: Dict[str, Any], character_id: str) -> Dict[str, Any] | None:
    row = store.get("npc_to_pov", {}).get(str(character_id))
    return deepcopy(row) if isinstance(row, dict) else None


def scene_snapshot(store: Dict[str, Any], participant_ids: Iterable[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    npc_to_pov = store.get("npc_to_pov") if isinstance(store.get("npc_to_pov"), dict) else {}
    pov_id = str(store.get("pov_character_id") or "")
    for character_id in participant_ids:
        cid = str(character_id)
        if not cid or cid == pov_id:
            continue
        row = npc_to_pov.get(cid)
        result[cid] = deepcopy(row) if isinstance(row, dict) else {"dimensions": {}}
    return result


def npc_network(store: Dict[str, Any]) -> Dict[str, Any]:
    relations: List[Dict[str, Any]] = []
    raw = store.get("npc_to_npc") if isinstance(store.get("npc_to_npc"), dict) else {}
    for owner_id, targets in raw.items():
        if not isinstance(targets, dict):
            continue
        for target_id, description in targets.items():
            relations.append({
                "owner_character_id": str(owner_id),
                "target_character_id": str(target_id),
                "description": str(description),
            })
    return {
        "director_only": True,
        "directional": True,
        "always_read": True,
        "relations": relations,
        "instruction": "NPC↔NPC только качественные направленные связи без числовых шкал.",
    }


def outgoing_npc_relations(store: Dict[str, Any], character_id: str) -> List[Dict[str, str]]:
    targets = store.get("npc_to_npc", {}).get(str(character_id), {})
    if not isinstance(targets, dict):
        return []
    return [
        {
            "owner_character_id": str(character_id),
            "target_character_id": str(target_id),
            "description": str(description),
        }
        for target_id, description in targets.items()
        if str(description).strip()
    ]


def apply_npc_updates(
    store: Dict[str, Any],
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    pov_id: str,
) -> Dict[str, Any]:
    if not isinstance(updates, list) or not updates:
        return deepcopy(store)
    result = deepcopy(store)
    target_store = result.setdefault("npc_to_npc", {})
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = _resolve_character_id(cards, raw.get("owner_character_id"))
        target_id = _resolve_character_id(cards, raw.get("target_character_id"))
        if not owner_id or not target_id or owner_id == target_id or owner_id == pov_id or target_id == pov_id:
            raise ValueError("NPC_RELATIONSHIP_INVALID_PAIR")
        text = _qualitative_relation(raw)
        if not text:
            text = " ".join(str(raw.get("description") or "").split()[:MAX_RELATION_WORDS])
        if text:
            target_store.setdefault(owner_id, {})[target_id] = text
    return result


def ensure_participant_records(store: Dict[str, Any], participant_ids: Iterable[str], cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Remember an encounter without inventing a numeric attitude."""
    result = deepcopy(store)
    known_ids = {storage._card_id(card) for card in cards}
    for cid in participant_ids:
        if cid in known_ids and cid != result.get("pov_character_id"):
            result["npc_to_pov"].setdefault(cid, {"dimensions": {}})
    return result


def apply_updates(
    store: Dict[str, Any], updates: Any, *, cards: List[Dict[str, Any]],
    pov_id: str, turn_number: int, participant_ids: Iterable[str],
    enforce_turn_invariants: bool = True,
) -> Dict[str, Any]:
    """Apply numeric changes; narrative judgment stays with the writer.

    No reason/scale/review gate and no per-turn change history. Structural bounds
    protect the file; an empty update or a value already at its bound is a no-op.
    """
    result = normalize_store(store, pov_id)
    participants = {str(cid) for cid in participant_ids}
    for raw in updates if isinstance(updates, list) else []:
        if not isinstance(raw, dict):
            continue
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id or owner_id == pov_id or owner_id not in participants:
            raise ValueError("RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC")
        dimensions = result["npc_to_pov"].setdefault(owner_id, {"dimensions": {}})["dimensions"]
        pending = [item for item in raw.get("dimensions", []) or [] if isinstance(item, dict)]
        # Removing an existing axis frees a slot for a new one in the same update.
        pending.sort(key=lambda item: not any(_norm(label) == _norm(item.get("label")) for label in dimensions))
        for item in pending:
            label = " ".join(str(item.get("label") or "").split())
            if not label:
                continue
            existing = next((name for name in dimensions if _norm(name) == _norm(label)), None)
            if existing is not None:
                delta = item.get("delta")
                if not _is_number(delta):
                    raise ValueError("RELATIONSHIP_EXISTING_DIMENSION_DELTA_REQUIRED")
                value = _bounded_value(dimensions[existing]["value"] + delta)
                if value == 0:
                    dimensions.pop(existing)
                else:
                    dimensions[existing] = {"value": value}
            else:
                value = item.get("value")
                if not _is_number(value):
                    raise ValueError("RELATIONSHIP_NEW_DIMENSION_VALUE_REQUIRED")
                value = _bounded_value(value)
                if value > 0:
                    if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                        raise ValueError("RELATIONSHIP_DIMENSION_LIMIT")
                    dimensions[label] = {"value": value}
    return result


def _historical_updates_for_replay(
    store: Dict[str, Any],
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(updates, list):
        return []

    result: List[Dict[str, Any]] = []
    npc_to_pov = store.get("npc_to_pov") if isinstance(store.get("npc_to_pov"), dict) else {}

    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            continue
        owner = npc_to_pov.get(owner_id) if isinstance(npc_to_pov.get(owner_id), dict) else {}
        saved_dimensions = owner.get("dimensions") if isinstance(owner.get("dimensions"), dict) else {}

        existing_changes: List[Dict[str, Any]] = []
        new_dimensions: List[Dict[str, Any]] = []
        seen: set[str] = set()
        critical = False
        freed_slots = 0

        for item in raw.get("dimensions", []) if isinstance(raw.get("dimensions"), list) else []:
            if not isinstance(item, dict):
                continue
            label = " ".join(str(item.get("label") or "").split())
            key = _norm(label)
            if not label or not key or key in seen:
                continue
            seen.add(key)

            existing_label = next(
                (name for name in saved_dimensions if _norm(name) == key),
                None,
            )

            if existing_label is not None:
                current_value = float(saved_dimensions[existing_label]["value"])
                delta = item.get("delta")
                if _is_number(delta) and float(delta) != 0.0:
                    replay_delta = float(delta)
                elif _is_number(item.get("value")):
                    target = max(0.0, min(100.0, float(item["value"])))
                    replay_delta = target - current_value
                else:
                    continue
                if replay_delta == 0.0:
                    continue
                if current_value + replay_delta <= 0.0:
                    freed_slots += 1
                existing_changes.append({
                    "label": existing_label,
                    "delta": _number(replay_delta),
                })
                critical = critical or abs(replay_delta) > ORDINARY_DELTA_LIMIT
                continue

            value = item.get("value")
            if not _is_number(value) or float(value) <= 0.0:
                continue
            replay_value = min(100.0, float(value))
            new_dimensions.append({
                "label": label,
                "value": _number(replay_value),
            })
            critical = critical or replay_value > ORDINARY_DELTA_LIMIT

        free_slots = max(
            0,
            MAX_DIMENSIONS_PER_NPC - len(saved_dimensions) + freed_slots,
        )
        dimensions = existing_changes + new_dimensions[:free_slots]

        dynamic = " ".join(str(raw.get("dynamic") or "").split())[:700]
        if not dimensions and not dynamic:
            continue

        row: Dict[str, Any] = {
            "character_id": owner_id,
            "reason": " ".join(str(raw.get("reason") or "").split())[:360] or "историческая запись",
        }
        if dimensions:
            row["dimensions"] = dimensions
        if dynamic:
            row["dynamic"] = dynamic
        raw_scale = str(raw.get("change_scale") or "ordinary").strip().casefold()
        row["change_scale"] = "critical_event" if critical else (
            raw_scale if raw_scale in {"ordinary", "critical_event"} else "ordinary"
        )
        result.append(row)

    return result


def rebuild_from_turns(
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    turns: List[Dict[str, Any]],
) -> Dict[str, Any]:
    starting_state = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    pov_id = str((starting_state.get("pov") or {}).get("character_id") or "")
    if not pov_id:
        pov_id = str(storage._find_pov_id(source, cards) or "")
    store = build_initial_store(cards, starting_state, pov_id)
    all_ids = [storage._card_id(card) for card in cards if storage._card_id(card)]

    working_state = deepcopy(starting_state)
    from .turn_pipeline import _relationship_scene_participants

    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_number = int(turn.get("turn_number", 0) or 0)
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        after = storage._deep_merge(working_state, extracted.get("state_patch") or {})
        participants = _relationship_scene_participants(
            working_state,
            after,
            extracted,
            cards=cards,
            user_input=str(turn.get("user_input") or ""),
            scene_output=str(turn.get("scene_output") or ""),
        )
        store = ensure_participant_records(store, participants, cards)
        working_state = after
        store = apply_npc_updates(
            store,
            extracted.get("npc_relationship_updates"),
            cards=cards,
            pov_id=pov_id,
        )
        replay_updates = _historical_updates_for_replay(
            store,
            extracted.get("relationship_updates"),
            cards=cards,
        )
        store = apply_updates(
            store,
            replay_updates,
            cards=cards,
            pov_id=pov_id,
            turn_number=turn_number,
            participant_ids=all_ids,
            enforce_turn_invariants=False,
        )
    return store


def footer_rows(store: Dict[str, Any], physical_ids: Iterable[str]) -> Dict[str, Dict[str, int | float]]:
    result: Dict[str, Dict[str, int | float]] = {}
    for character_id in physical_ids:
        row = character_relation(store, str(character_id))
        if not row:
            continue
        dims = row.get("dimensions") if isinstance(row.get("dimensions"), dict) else {}
        values = {
            str(label): item.get("value")
            for label, item in dims.items()
            if isinstance(item, dict) and _is_number(item.get("value")) and float(item.get("value")) != 0.0
        }
        if values:
            result[str(character_id)] = values
    return result
