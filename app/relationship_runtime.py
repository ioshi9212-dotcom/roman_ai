from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List


_METRIC_RE = re.compile(
    r"^(.+?)\s+(-?\d+(?:\.\d+)?)(?:\s*/\s*([+-]?\d+(?:\.\d+)?))?$"
)
MAX_DIMENSIONS = 8


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: str) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _norm(value: Any) -> str:
    return " ".join(str(value).casefold().replace("ё", "е").split())


def _dimension_key(label: str) -> str:
    return _norm(label).replace(" ", "_")


def _resolve_owner(
    cards: Iterable[Dict[str, Any]],
    value: Any,
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "->" in text:
        text = text.split("->", 1)[0].strip()
    return resolve_character_id(cards, text) or text


def _canonical_flat(
    value: Any,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, dict):
        return result
    for raw_owner, raw_relation in value.items():
        owner_id = _resolve_owner(cards, raw_owner, resolve_character_id)
        if not owner_id or not isinstance(raw_relation, dict):
            continue
        bucket = result.setdefault(owner_id, {})
        bucket.update(deepcopy(raw_relation))
    return result


def _canonical_schemas(
    value: Any,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    if not isinstance(value, dict):
        return result
    for raw_owner, raw_schema in value.items():
        owner_id = _resolve_owner(cards, raw_owner, resolve_character_id)
        if not owner_id or not isinstance(raw_schema, list):
            continue
        labels = [str(item).strip() for item in raw_schema if str(item).strip()]
        if labels:
            result[owner_id] = labels[:MAX_DIMENSIONS]
    return result


def _dimensions_from_flat(
    relation: Any,
    *,
    preferred_labels: List[str] | None = None,
) -> List[Dict[str, Any]]:
    if not isinstance(relation, dict):
        return []
    numeric = {str(key): value for key, value in relation.items() if _is_number(value)}
    if preferred_labels:
        by_norm = {_norm(key): (key, value) for key, value in numeric.items()}
        ordered = []
        for label in preferred_labels:
            match = by_norm.get(_norm(label))
            if match:
                ordered.append(match)
        if ordered:
            numeric = {key: value for key, value in ordered}
    return [
        {"key": _dimension_key(label), "label": label, "value": value}
        for label, value in list(numeric.items())[:MAX_DIMENSIONS]
    ]


def _normalise_dimensions(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip()
        metric_value = raw.get("value")
        if not label or not _is_number(metric_value):
            continue
        key = str(raw.get("key") or _dimension_key(label)).strip()
        identity = _norm(key)
        if not identity or identity in seen:
            continue
        result.append({"key": key, "label": label, "value": metric_value})
        seen.add(identity)
        if len(result) >= MAX_DIMENSIONS:
            break
    return result


def _empty_relation(pov_id: str, dimensions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "target_character_id": pov_id,
        "relationship_type": "установленная связь" if dimensions else "не установлено",
        "relationship_context": "",
        "current_dynamic": "",
        "dimensions": deepcopy(dimensions),
        "beliefs_about_target": [],
        "unresolved_between_them": [],
        "dynamic_constraints": [],
        "change_reasons": [],
        "last_changed_turn": 0,
    }


def _parse_footer(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(scene_output, str):
        return {}
    lines = scene_output.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Отношения:") + 1
    except StopIteration:
        return {}

    result: Dict[str, List[Dict[str, Any]]] = {}
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Ход "):
            break
        if " - " not in line:
            continue
        raw_name, raw_metrics = line.split(" - ", 1)
        owner_id = resolve_character_id(cards, raw_name.strip())
        if not owner_id:
            continue
        metrics: List[Dict[str, Any]] = []
        for raw_metric in raw_metrics.split(";"):
            match = _METRIC_RE.match(raw_metric.strip())
            if not match:
                continue
            label = match.group(1).strip()
            if not label:
                continue
            metrics.append(
                {
                    "key": _dimension_key(label),
                    "label": label,
                    "value": _number(match.group(2)),
                    "delta": _number(match.group(3)) if match.group(3) is not None else None,
                }
            )
        if metrics:
            result[str(owner_id)] = metrics[:MAX_DIMENSIONS]
    return result


def _latest_footer(
    turns: List[Dict[str, Any]],
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        result.update(
            _parse_footer(
                turn.get("scene_output", ""),
                cards=cards,
                resolve_character_id=resolve_character_id,
            )
        )
    return result


def _canonical_docs(
    state: Dict[str, Any],
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Dict[str, Any]]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    docs: Dict[str, Dict[str, Any]] = {}
    raw_docs = state.get("relationship_documents")
    if not isinstance(raw_docs, dict):
        return docs

    for raw_owner, raw_doc in raw_docs.items():
        owner_id = _resolve_owner(cards, raw_owner, resolve_character_id)
        if not owner_id or owner_id == pov_id or not isinstance(raw_doc, dict):
            continue
        relations = []
        for raw_relation in raw_doc.get("relations", []) if isinstance(raw_doc.get("relations"), list) else []:
            if not isinstance(raw_relation, dict):
                continue
            target_id = resolve_character_id(cards, raw_relation.get("target_character_id")) or str(
                raw_relation.get("target_character_id") or ""
            )
            if not target_id:
                continue
            relation = deepcopy(raw_relation)
            relation["target_character_id"] = target_id
            relation["dimensions"] = _normalise_dimensions(relation.get("dimensions"))
            relation.setdefault("relationship_type", "установленная связь")
            relation.setdefault("relationship_context", "")
            relation.setdefault("current_dynamic", "")
            relation.setdefault("beliefs_about_target", [])
            relation.setdefault("unresolved_between_them", [])
            relation.setdefault("dynamic_constraints", [])
            relation.setdefault("change_reasons", [])
            relation.setdefault("last_changed_turn", 0)
            relations.append(relation)
        docs[owner_id] = {"owner_character_id": owner_id, "relations": relations}
    return docs


def _pov_relation(doc: Dict[str, Any], pov_id: str) -> Dict[str, Any] | None:
    for relation in doc.get("relations", []) if isinstance(doc, dict) else []:
        if isinstance(relation, dict) and str(relation.get("target_character_id")) == pov_id:
            return relation
    return None


def _sync_state(state: Dict[str, Any], docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    result = deepcopy(state)
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    flat: Dict[str, Dict[str, Any]] = {}

    for owner_id, doc in docs.items():
        relation = _pov_relation(doc, pov_id)
        if relation is None:
            continue
        metrics = {
            str(item.get("label") or item.get("key")): item.get("value")
            for item in _normalise_dimensions(relation.get("dimensions"))
        }
        if metrics:
            flat[owner_id] = metrics

    result["relationships"] = flat
    result["relationship_documents"] = deepcopy(docs)
    result.pop("relationship_schemas", None)
    return result


def repair_relationship_state(
    state: Dict[str, Any],
    *,
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    """Migrate current sessions to the old generator's directed relationship document model."""
    result = deepcopy(state if isinstance(state, dict) else {})
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    flat = _canonical_flat(
        result.get("relationships", {}),
        cards=cards,
        resolve_character_id=resolve_character_id,
    )
    legacy_schemas = _canonical_schemas(
        result.get("relationship_schemas", {}),
        cards=cards,
        resolve_character_id=resolve_character_id,
    )
    docs = _canonical_docs(result, cards=cards, resolve_character_id=resolve_character_id)
    latest = _latest_footer(turns, cards=cards, resolve_character_id=resolve_character_id)

    owner_ids = set(flat) | set(docs) | set(latest)
    for owner_id in owner_ids:
        if owner_id == pov_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = _pov_relation(doc, pov_id)
        if relation is None and pov_id:
            relation = _empty_relation(pov_id, [])
            doc["relations"].append(relation)
        if relation is None:
            continue

        dimensions = _normalise_dimensions(relation.get("dimensions"))
        if not dimensions and owner_id in flat:
            dimensions = _dimensions_from_flat(
                flat[owner_id], preferred_labels=legacy_schemas.get(owner_id)
            )
        if not dimensions and owner_id in latest:
            dimensions = [
                {"key": item["key"], "label": item["label"], "value": item["value"]}
                for item in latest[owner_id]
            ][:MAX_DIMENSIONS]
        relation["dimensions"] = dimensions

    return _sync_state(result, docs)


def build_relationship_lens(
    state: Dict[str, Any],
    *,
    cards: Iterable[Dict[str, Any]],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    docs = _canonical_docs(state, cards=cards, resolve_character_id=resolve_character_id)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    names: Dict[str, str] = {}
    for card in cards:
        cid = str(card.get("character_id") or card.get("id") or card.get("name") or "")
        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        if cid:
            names[cid] = str(card.get("name") or card.get("full_name") or identity.get("name") or cid)

    relations = []
    for owner_id in dict.fromkeys(present_character_ids(state)):
        if owner_id == pov_id:
            continue
        relation = _pov_relation(docs.get(owner_id, {}), pov_id)
        if relation is None:
            continue
        relations.append(
            {
                "owner_character_id": owner_id,
                "owner_name": names.get(owner_id, owner_id),
                "target_character_id": pov_id,
                "target_name": names.get(pov_id, pov_id),
                "relationship_type": relation.get("relationship_type"),
                "relationship_context": relation.get("relationship_context"),
                "current_dynamic": relation.get("current_dynamic"),
                "dimensions": _normalise_dimensions(relation.get("dimensions")),
                "beliefs_about_target": deepcopy(relation.get("beliefs_about_target", [])),
                "unresolved_between_them": deepcopy(relation.get("unresolved_between_them", [])),
                "dynamic_constraints": deepcopy(relation.get("dynamic_constraints", [])),
                "last_changed_turn": int(relation.get("last_changed_turn", 0) or 0),
            }
        )

    return {
        "relations_in_current_scene": relations,
        "instruction": (
            "Relationship dimensions are causal state, not decorative footer numbers. Combine them with personality, goals, knowledge and current state. Absence of a dimension is not zero. Do not use interest as a generic fallback. Preserve established dimensions across absences and later meetings. For every present NPC with saved dimensions, show those dimensions in the visible Relationships footer even when delta is zero."
        ),
    }


def relationship_snapshot_for_present(
    state: Dict[str, Any],
    *,
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    result = {}
    for owner_id in present_character_ids(state):
        if owner_id == pov_id:
            continue
        relation = relationships.get(owner_id)
        if isinstance(relation, dict):
            metrics = {str(key): value for key, value in relation.items() if _is_number(value)}
            if metrics:
                result[owner_id] = {"metrics": metrics}
    return result


def _merge_footer_dimensions(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    result = _normalise_dimensions(existing)
    if not result:
        return [
            {"key": item["key"], "label": item["label"], "value": item["value"]}
            for item in incoming[:MAX_DIMENSIONS]
        ]

    by_label = {_norm(item["label"]): index for index, item in enumerate(result)}
    incoming_labels = {_norm(item["label"]) for item in incoming}
    if incoming_labels and incoming_labels.isdisjoint(set(by_label)):
        # A reunion must not silently replace the stored relationship with fresh vocabulary.
        return result

    for item in incoming:
        key = _norm(item["label"])
        if key in by_label:
            result[by_label[key]]["value"] = item["value"]
            continue
        if len(result) < MAX_DIMENSIONS:
            result.append(
                {"key": item["key"], "label": item["label"], "value": item["value"]}
            )
            by_label[key] = len(result) - 1
    return result


def relationship_patch_from_scene(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    state: Dict[str, Any],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    footer = _parse_footer(
        scene_output,
        cards=cards,
        resolve_character_id=resolve_character_id,
    )
    if not footer:
        return {}

    result = deepcopy(state)
    docs = _canonical_docs(result, cards=cards, resolve_character_id=resolve_character_id)
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(present_character_ids(result))

    for owner_id, incoming in footer.items():
        if owner_id == pov_id or owner_id not in present:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = _pov_relation(doc, pov_id)
        if relation is None:
            relation = _empty_relation(pov_id, [])
            doc["relations"].append(relation)
        before = _normalise_dimensions(relation.get("dimensions"))
        after = _merge_footer_dimensions(before, incoming)
        relation["dimensions"] = after
        if after != before:
            relation["last_changed_turn"] = int(relation.get("last_changed_turn", 0) or 0) + 1

    updated = _sync_state(result, docs)
    patch: Dict[str, Any] = {}
    for key in ("relationships", "relationship_documents"):
        if updated.get(key) != state.get(key):
            patch[key] = deepcopy(updated.get(key, {}))
    return patch


def overwrite_relationship_snapshots(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    # Storage already applies the full state_patch. This compatibility step must not delete docs.
    result = deepcopy(state if isinstance(state, dict) else {})
    if isinstance(patch.get("relationships"), dict):
        result["relationships"] = deepcopy(patch["relationships"])
    if isinstance(patch.get("relationship_documents"), dict):
        result["relationship_documents"] = deepcopy(patch["relationship_documents"])
    result.pop("relationship_schemas", None)
    return result
