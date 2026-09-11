from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Tuple


_METADATA_KEYS = (
    "opinion",
    "current_dynamic",
    "beliefs_about_target",
    "unresolved_between_them",
    "relationship_type",
    "relationship_context",
)


def apply_relationship_metadata(
    state: Dict[str, Any],
    rows: Iterable[Dict[str, Any]] | None,
    *,
    turn_number: int,
) -> Tuple[Dict[str, Any], bool]:
    result = deepcopy(state if isinstance(state, dict) else {})
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return result, False

    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    if not pov_id:
        return result, False

    docs = result.get("relationship_documents")
    docs = deepcopy(docs) if isinstance(docs, dict) else {}
    changed = False

    for raw in rows:
        owner_id = str(raw.get("character_id") or "")
        if not owner_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relations = doc.get("relations")
        if not isinstance(relations, list):
            relations = []
            doc["relations"] = relations
        relation = next(
            (
                row for row in relations
                if isinstance(row, dict)
                and str(row.get("target_character_id") or "") == pov_id
            ),
            None,
        )
        if relation is None:
            relation = {
                "target_character_id": pov_id,
                "relationship_type": "установленная связь",
                "relationship_context": "",
                "current_dynamic": "",
                "dimensions": [],
                "beliefs_about_target": [],
                "unresolved_between_them": [],
                "dynamic_constraints": [],
                "change_reasons": [],
                "last_changed_turn": 0,
            }
            relations.append(relation)

        before = deepcopy(relation)
        if "opinion" in raw:
            relation["current_dynamic"] = str(raw.get("opinion") or "").strip()
        if "current_dynamic" in raw:
            relation["current_dynamic"] = str(raw.get("current_dynamic") or "").strip()
        for key in ("beliefs_about_target", "unresolved_between_them"):
            if key in raw and isinstance(raw[key], list):
                relation[key] = deepcopy(raw[key])
        for key in ("relationship_type", "relationship_context"):
            if key in raw:
                relation[key] = str(raw.get(key) or "").strip()
        if relation != before:
            relation["last_changed_turn"] = int(turn_number)
            changed = True

    if changed:
        result["relationship_documents"] = docs
    return result, changed


def metadata_rows_from_payload(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    rows = payload.get("_relationship_metadata")
    return [deepcopy(row) for row in rows] if isinstance(rows, list) else []
