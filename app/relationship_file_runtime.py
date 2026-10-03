from __future__ import annotations

from copy import deepcopy
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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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
        "version": 1,
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
                dimensions: Dict[str, Dict[str, Any]] = {}
                for raw_dim in row.get("dimensions", []) if isinstance(row.get("dimensions"), list) else []:
                    if not isinstance(raw_dim, dict):
                        continue
                    label = " ".join(str(raw_dim.get("label") or raw_dim.get("key") or "").split())
                    value = raw_dim.get("value")
                    if not label or not _is_number(value) or float(value) == 0.0:
                        continue
                    if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                        break
                    dimensions[label] = {
                        "value": _number(value),
                        "last_delta": 0,
                        "last_turn": 0,
                        "reason": _reason_from_relation(row),
                    }
                if dimensions:
                    npc_to_pov[owner_id] = {"dimensions": dimensions}
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


def _migrate_legacy_state(store: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(store)
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    npc_to_pov = result.setdefault("npc_to_pov", {})

    for owner_id, raw in flat.items():
        if not isinstance(raw, dict):
            continue
        dimensions: Dict[str, Dict[str, Any]] = {}
        for label, value in raw.items():
            if not _is_number(value) or float(value) == 0.0:
                continue
            dimensions[str(label)] = {
                "value": _number(value),
                "last_delta": 0,
                "last_turn": 0,
                "reason": _legacy_reason(state, str(owner_id), str(label)),
            }
            if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                break
        if dimensions:
            npc_to_pov[str(owner_id)] = {"dimensions": dimensions}

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
            if len(clean) >= MAX_DIMENSIONS_PER_NPC or not isinstance(item, dict):
                break
            value = item.get("value")
            if not _is_number(value) or float(value) == 0.0:
                continue
            clean[str(label)] = {
                "value": _number(value),
                "last_delta": _number(item.get("last_delta")) if _is_number(item.get("last_delta")) else 0,
                "last_turn": int(item.get("last_turn", 0) or 0),
                "reason": " ".join(str(item.get("reason") or "").split())[:360],
            }
        if clean:
            result["npc_to_pov"][str(owner_id)] = {"dimensions": clean}

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
        return normalize_store(storage._read_json(path, {}), pov_id)
    store = build_initial_store(cards, state, pov_id)
    storage._write_json(path, store)
    return store


def character_relation(store: Dict[str, Any], character_id: str) -> Dict[str, Any] | None:
    row = store.get("npc_to_pov", {}).get(str(character_id))
    return deepcopy(row) if isinstance(row, dict) else None


def scene_snapshot(store: Dict[str, Any], participant_ids: Iterable[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    npc_to_pov = store.get("npc_to_pov") if isinstance(store.get("npc_to_pov"), dict) else {}
    for character_id in participant_ids:
        cid = str(character_id)
        row = npc_to_pov.get(cid)
        if isinstance(row, dict):
            result[cid] = deepcopy(row)
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


def apply_updates(
    store: Dict[str, Any],
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    pov_id: str,
    turn_number: int,
    participant_ids: Iterable[str],
) -> Dict[str, Any]:
    if not isinstance(updates, list) or not updates:
        return deepcopy(store)

    result = deepcopy(store)
    npc_to_pov = result.setdefault("npc_to_pov", {})
    participants = {str(value) for value in participant_ids if value}

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

        owner = npc_to_pov.setdefault(owner_id, {"dimensions": {}})
        dimensions = owner.setdefault("dimensions", {})
        if not isinstance(dimensions, dict):
            dimensions = {}
            owner["dimensions"] = dimensions

        seen: set[str] = set()
        for item in raw.get("dimensions", []) if isinstance(raw.get("dimensions"), list) else []:
            if not isinstance(item, dict):
                continue
            label = " ".join(str(item.get("label") or "").split())
            key = _norm(label)
            if not label or not key or key in seen:
                raise ValueError("RELATIONSHIP_DIMENSION_INVALID")
            seen.add(key)

            existing_label = next((name for name in dimensions if _norm(name) == key), None)
            if existing_label is not None:
                delta = item.get("delta")
                if not _is_number(delta) or float(delta) == 0.0:
                    raise ValueError("RELATIONSHIP_EXISTING_DIMENSION_DELTA_REQUIRED")
                if scale == "ordinary" and abs(float(delta)) > ORDINARY_DELTA_LIMIT:
                    raise ValueError("RELATIONSHIP_ORDINARY_DELTA_LIMIT")
                new_value = float(dimensions[existing_label]["value"]) + float(delta)
                if new_value == 0.0:
                    dimensions.pop(existing_label, None)
                    continue
                dimensions[existing_label] = {
                    "value": _number(new_value),
                    "last_delta": _number(delta),
                    "last_turn": int(turn_number),
                    "reason": reason,
                }
                continue

            value = item.get("value")
            if not _is_number(value) or float(value) == 0.0:
                raise ValueError("RELATIONSHIP_NEW_DIMENSION_VALUE_REQUIRED")
            if scale == "ordinary" and abs(float(value)) > ORDINARY_DELTA_LIMIT:
                raise ValueError("RELATIONSHIP_ORDINARY_DELTA_LIMIT")
            if len(dimensions) >= MAX_DIMENSIONS_PER_NPC:
                raise ValueError("RELATIONSHIP_DIMENSION_LIMIT")
            dimensions[label] = {
                "value": _number(value),
                "last_delta": _number(value),
                "last_turn": int(turn_number),
                "reason": reason,
            }

        if not dimensions:
            npc_to_pov.pop(owner_id, None)

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

    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_number = int(turn.get("turn_number", 0) or 0)
        extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
        store = apply_npc_updates(
            store,
            extracted.get("npc_relationship_updates"),
            cards=cards,
            pov_id=pov_id,
        )
        store = apply_updates(
            store,
            extracted.get("relationship_updates"),
            cards=cards,
            pov_id=pov_id,
            turn_number=turn_number,
            participant_ids=all_ids,
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
