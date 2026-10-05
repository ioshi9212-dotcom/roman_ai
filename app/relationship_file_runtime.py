from __future__ import annotations

from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from . import storage


FILE_NAME = "relationships.json"
MAX_DIMENSIONS_PER_NPC = 10
ORDINARY_DELTA_LIMIT = 3.0


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _bounded_value(value: Any) -> int | float:
    number = max(0.0, min(100.0, float(value)))
    return _number(number)


def _change(turn: int, delta: Any, reason: str) -> Dict[str, Any]:
    return {
        "turn": int(turn),
        "delta": _number(delta) if _is_number(delta) else 0,
        "reason": " ".join(str(reason or "").split())[:360],
    }


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


def _reason_from_relation(row: Dict[str, Any]) -> str:
    for key in ("relationship_context", "current_dynamic", "relationship_type", "behavioral_pattern"):
        value = " ".join(str(row.get(key) or "").split())
        if value:
            return value[:360]
    return "из стартовой анкеты"


def _qualitative_relation(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("relationship_type", "relationship_context", "current_dynamic", "behavioral_pattern"):
        value = " ".join(str(row.get(key) or "").split())
        if value and value not in parts:
            parts.append(value)
    return " | ".join(parts)[:700]


def _empty_store(pov_id: str) -> Dict[str, Any]:
    return {
        "version": 2,
        "pov_character_id": str(pov_id or ""),
        "npc_to_pov": {},
        "npc_to_npc": {},
    }


def _profile_store(cards: List[Dict[str, Any]], pov_id: str) -> Dict[str, Any]:
    store = _empty_store(pov_id)
    npc_to_pov = store["npc_to_pov"]
    npc_to_npc = store["npc_to_npc"]

    for card in cards:
        owner_id = storage._card_id(card)
        if not owner_id or owner_id == pov_id:
            continue
        for row in _relationship_rows(card):
            target_id = _target_id(cards, row)
            if not target_id or target_id == owner_id:
                continue
            if target_id == pov_id:
                owner = npc_to_pov.setdefault(owner_id, {"dimensions": {}})
                dynamic = " ".join(str(row.get("current_dynamic") or "").split())[:700]
                if dynamic:
                    owner["dynamic"] = dynamic
                    owner["dynamic_last_change"] = _change(0, 0, _reason_from_relation(row))
                dimensions = owner.setdefault("dimensions", {})
                if not isinstance(dimensions, dict):
                    dimensions = {}
                    owner["dimensions"] = dimensions
                for raw_dim in row.get("dimensions", []) if isinstance(row.get("dimensions"), list) else []:
                    if not isinstance(raw_dim, dict):
                        continue
                    label = " ".join(str(raw_dim.get("label") or raw_dim.get("key") or "").split())
                    value = raw_dim.get("value")
                    if not label or not _is_number(value) or float(value) <= 0.0:
                        continue
                    existing_label = next((name for name in dimensions if _norm(name) == _norm(label)), None)
                    if existing_label is not None:
                        dimensions[existing_label] = {
                            "value": _bounded_value(value),
                            "last_change": _change(0, 0, _reason_from_relation(row)),
                        }
                        continue
                    if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                        break
                    dimensions[label] = {
                        "value": _bounded_value(value),
                        "last_change": _change(0, 0, _reason_from_relation(row)),
                    }
                if not dimensions and not owner.get("dynamic"):
                    npc_to_pov.pop(owner_id, None)
            else:
                text = _qualitative_relation(row)
                if text:
                    npc_to_npc.setdefault(owner_id, {})[target_id] = text
    return store


def _legacy_reason(state: Dict[str, Any], owner_id: str, label: str) -> str:
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    doc = docs.get(owner_id) if isinstance(docs.get(owner_id), dict) else {}
    reasons: List[Dict[str, Any]] = []
    for relation in doc.get("relations", []) if isinstance(doc.get("relations"), list) else []:
        if not isinstance(relation, dict):
            continue
        rows = relation.get("change_reasons")
        if isinstance(rows, list):
            reasons.extend(row for row in rows if isinstance(row, dict))
    norm_label = _norm(label)
    for row in reversed(reasons):
        changes = row.get("changes") if isinstance(row.get("changes"), list) else []
        if any(_norm(item.get("label") or item.get("key")) == norm_label for item in changes if isinstance(item, dict)):
            reason = " ".join(str(row.get("reason") or "").split())
            if reason:
                return reason[:360]
    return "перенесено из существующей сессии"


def _legacy_dynamic(state: Dict[str, Any], owner_id: str, pov_id: str) -> str:
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    doc = docs.get(owner_id) if isinstance(docs.get(owner_id), dict) else {}
    for relation in doc.get("relations", []) if isinstance(doc.get("relations"), list) else []:
        if not isinstance(relation, dict):
            continue
        target = str(relation.get("target_character_id") or relation.get("target_id") or relation.get("target") or "")
        if target and _norm(target) != _norm(pov_id):
            continue
        text = " ".join(str(relation.get("current_dynamic") or "").split())[:700]
        if text:
            return text
    return ""


def _migrate_legacy_state(store: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(store)
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    npc_to_pov = result.setdefault("npc_to_pov", {})

    for owner_id, raw in flat.items():
        if not isinstance(raw, dict):
            continue
        dimensions: Dict[str, Dict[str, Any]] = {}
        for label, value in raw.items():
            if not _is_number(value) or float(value) <= 0.0:
                continue
            dimensions[str(label)] = {
                "value": _bounded_value(value),
                "last_change": _change(0, 0, _legacy_reason(state, str(owner_id), str(label))),
            }
            if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                break
        existing = npc_to_pov.get(str(owner_id)) if isinstance(npc_to_pov.get(str(owner_id)), dict) else {}
        dynamic = (
            _legacy_dynamic(state, str(owner_id), str(result.get("pov_character_id") or ""))
            or " ".join(str(existing.get("dynamic") or "").split())[:700]
        )
        if dimensions or dynamic:
            npc_to_pov[str(owner_id)] = {"dimensions": dimensions}
            if dynamic:
                npc_to_pov[str(owner_id)]["dynamic"] = dynamic

    legacy_npc = state.get("npc_relationships") if isinstance(state.get("npc_relationships"), dict) else {}
    npc_to_npc = result.setdefault("npc_to_npc", {})
    for owner_id, targets in legacy_npc.items():
        if not isinstance(targets, dict):
            continue
        for target_id, raw in targets.items():
            if not isinstance(raw, dict):
                continue
            text = _qualitative_relation(raw)
            if text:
                npc_to_npc.setdefault(str(owner_id), {})[str(target_id)] = text
    return result


def normalize_store(value: Any, pov_id: str = "") -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = _empty_store(str(source.get("pov_character_id") or pov_id or ""))
    npc_to_pov = source.get("npc_to_pov") if isinstance(source.get("npc_to_pov"), dict) else {}
    for owner_id, raw in npc_to_pov.items():
        if not isinstance(raw, dict):
            continue
        dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else {}
        clean: Dict[str, Dict[str, Any]] = {}
        for label, item in dimensions.items():
            if len(clean) >= MAX_DIMENSIONS_PER_NPC:
                break
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not _is_number(value) or float(value) <= 0.0:
                continue
            last_change = item.get("last_change") if isinstance(item.get("last_change"), dict) else {}
            if not last_change:
                last_change = {
                    "turn": int(item.get("last_turn", 0) or 0),
                    "delta": item.get("last_delta", 0),
                    "reason": item.get("reason", ""),
                }
            clean[str(label)] = {
                "value": _bounded_value(value),
                "last_change": _change(
                    int(last_change.get("turn", 0) or 0),
                    last_change.get("delta", 0),
                    str(last_change.get("reason") or ""),
                ),
            }
        dynamic = " ".join(str(raw.get("dynamic") or "").split())[:700]
        if clean or dynamic or raw.get("dimensions") == {}:
            result["npc_to_pov"][str(owner_id)] = {"dimensions": clean}
            if dynamic:
                result["npc_to_pov"][str(owner_id)]["dynamic"] = dynamic
                change = raw.get("dynamic_last_change")
                if isinstance(change, dict):
                    result["npc_to_pov"][str(owner_id)]["dynamic_last_change"] = _change(
                        int(change.get("turn", 0) or 0), 0, str(change.get("reason") or "")
                    )

    npc_to_npc = source.get("npc_to_npc") if isinstance(source.get("npc_to_npc"), dict) else {}
    for owner_id, targets in npc_to_npc.items():
        if not isinstance(targets, dict):
            continue
        clean_targets: Dict[str, str] = {}
        for target_id, text in targets.items():
            value_text = " ".join(str(text or "").split())[:700]
            if value_text:
                clean_targets[str(target_id)] = value_text
        if clean_targets:
            result["npc_to_npc"][str(owner_id)] = clean_targets
    return result


def build_initial_store(cards: List[Dict[str, Any]], state: Dict[str, Any], pov_id: str) -> Dict[str, Any]:
    return _migrate_legacy_state(_profile_store(cards, pov_id), state)


def load(root: Path, *, cards: List[Dict[str, Any]], state: Dict[str, Any], pov_id: str) -> Dict[str, Any]:
    path = root / FILE_NAME
    if path.exists():
        raw = storage._read_json(path, {})
        store = normalize_store(raw, pov_id)
        if raw.get("version", 1) == 1:
            # Never relabel a played relationship with its outdated setup description.
            meta = storage._read_json(root / "meta.json", {})
            turns = storage._read_turns(root)
            if not turns and not int(meta.get("turn_number", 0) or 0):
                initial = _profile_store(cards, pov_id)
                for cid, row in store["npc_to_pov"].items():
                    initial_row = initial["npc_to_pov"].get(cid, {})
                    if not row.get("dynamic") and initial_row.get("dynamic"):
                        row["dynamic"] = initial_row["dynamic"]
                        row["dynamic_last_change"] = deepcopy(initial_row["dynamic_last_change"])
            for turn in turns:
                extracted = turn.get("extracted") or {}
                for update in extracted.get("relationship_updates") or []:
                    cid = _resolve_character_id(cards, update.get("character_id"))
                    row = store["npc_to_pov"].get(cid)
                    dynamic = " ".join(str(update.get("dynamic") or "").split())[:700]
                    if row is not None and dynamic:
                        row["dynamic"] = dynamic
                        row["dynamic_last_change"] = _change(
                            int(turn.get("turn_number", 0) or 0), 0, str(update.get("reason") or "")
                        )
            storage._write_json(path, store)
        elif raw != store:
            storage._write_json(path, store)
        return store

    store = build_initial_store(cards, state, pov_id)
    storage._write_json(path, store)

    cleaned_state = deepcopy(state)
    changed = False
    for key in ("relationships", "relationship_documents", "relationship_schemas", "npc_relationships"):
        if key in cleaned_state:
            cleaned_state.pop(key, None)
            changed = True
    if changed:
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
            text = " ".join(str(raw.get("description") or "").split())[:700]
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
    store: Dict[str, Any],
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    pov_id: str,
    turn_number: int,
    participant_ids: Iterable[str],
    enforce_turn_invariants: bool = True,
) -> Dict[str, Any]:
    if not isinstance(updates, list) or not updates:
        return deepcopy(store)

    result = deepcopy(store)
    npc_to_pov = result.setdefault("npc_to_pov", {})
    participants = {str(value) for value in participant_ids if value}
    changed_dimensions: set[tuple[str, str]] = set()

    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        owner_id = str(owner_id or "")
        if not owner_id or owner_id == pov_id or owner_id not in participants:
            raise ValueError("RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC")

        reason = " ".join(str(raw.get("reason") or "").split())[:360]
        if not reason:
            raise ValueError("RELATIONSHIP_CHANGE_REASON_REQUIRED")

        scale = str(raw.get("change_scale") or "ordinary").strip().casefold()
        if scale not in {"ordinary", "critical_event"}:
            raise ValueError("RELATIONSHIP_CHANGE_SCALE_INVALID")

        incoming_dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), list) else []
        incoming_dynamic = " ".join(str(raw.get("dynamic") or "").split())[:700]
        if not incoming_dimensions and not incoming_dynamic:
            raise ValueError("RELATIONSHIP_UPDATE_EMPTY")

        owner = npc_to_pov.setdefault(owner_id, {"dimensions": {}})
        dimensions = owner.setdefault("dimensions", {})
        if not isinstance(dimensions, dict):
            dimensions = {}
            owner["dimensions"] = dimensions

        changed_any = False
        bounded_noop = False
        if incoming_dynamic and incoming_dynamic != str(owner.get("dynamic") or ""):
            owner["dynamic"] = incoming_dynamic
            owner["dynamic_last_change"] = _change(turn_number, 0, reason)
            changed_any = True

        seen: set[str] = set()
        prepared_dimensions: List[Tuple[Dict[str, Any], str, str, str | None]] = []
        for item in incoming_dimensions:
            if not isinstance(item, dict):
                continue
            label = " ".join(str(item.get("label") or "").split())
            key = _norm(label)
            if not label or not key or key in seen:
                raise ValueError("RELATIONSHIP_DIMENSION_INVALID")
            seen.add(key)
            if enforce_turn_invariants and (owner_id, key) in changed_dimensions:
                raise ValueError("RELATIONSHIP_DIMENSION_DUPLICATE_UPDATE")
            changed_dimensions.add((owner_id, key))
            existing_label = next((name for name in dimensions if _norm(name) == key), None)
            prepared_dimensions.append((item, label, key, existing_label))

        # Apply existing axes first so an axis that reaches zero frees a slot
        # before a new axis from the same scene is created.
        prepared_dimensions.sort(key=lambda row: row[3] is None)

        for item, label, key, existing_label in prepared_dimensions:
            if existing_label is not None:
                delta = item.get("delta")
                if not _is_number(delta) or float(delta) == 0.0:
                    raise ValueError("RELATIONSHIP_EXISTING_DIMENSION_DELTA_REQUIRED")
                if scale == "ordinary" and abs(float(delta)) > ORDINARY_DELTA_LIMIT:
                    raise ValueError("RELATIONSHIP_ORDINARY_DELTA_LIMIT")
                current_value = float(dimensions[existing_label]["value"])
                raw_new_value = current_value + float(delta)
                if not isfinite(raw_new_value):
                    raise ValueError("RELATIONSHIP_VALUE_INVALID")
                new_value = max(0.0, min(100.0, raw_new_value))
                if new_value == 0.0:
                    dimensions.pop(existing_label, None)
                    changed_any = True
                    continue
                effective_delta = new_value - current_value
                if effective_delta == 0.0:
                    bounded_noop = True
                    continue
                dimensions[existing_label] = {
                    "value": _number(new_value),
                    "last_change": _change(turn_number, effective_delta, reason),
                }
                changed_any = True
                continue

            value = item.get("value")
            if not _is_number(value) or float(value) <= 0.0:
                raise ValueError("RELATIONSHIP_NEW_DIMENSION_VALUE_REQUIRED")
            if float(value) > 100.0:
                raise ValueError("RELATIONSHIP_VALUE_INVALID")
            if scale == "ordinary" and float(value) > ORDINARY_DELTA_LIMIT:
                raise ValueError("RELATIONSHIP_ORDINARY_DELTA_LIMIT")
            if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                raise ValueError("RELATIONSHIP_DIMENSION_LIMIT")
            dimensions[label] = {
                "value": _number(value),
                "last_change": _change(turn_number, value, reason),
            }
            changed_any = True

        if not changed_any and not bounded_noop:
            raise ValueError("RELATIONSHIP_UPDATE_EMPTY")
        if not dimensions and not owner.get("dynamic"):
            npc_to_pov.pop(owner_id, None)

    if any(len(row.get("dimensions", {})) > MAX_DIMENSIONS_PER_NPC for row in npc_to_pov.values()):
        raise ValueError("RELATIONSHIP_DIMENSION_LIMIT")
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
            working_state, after, extracted, cards=cards, user_input=str(turn.get("user_input") or "")
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
