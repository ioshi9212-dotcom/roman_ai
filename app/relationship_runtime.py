from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Tuple


# Old generator footer accepted both `доверие 10/+1` and `доверие 10`.
_METRIC_RE = re.compile(r"^(.+?)\s+(-?\d+(?:\.\d+)?)(?:\s*/\s*([+-]?\d+(?:\.\d+)?))?$")
MAX_DIMENSIONS = 8


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() else value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _norm(value: Any) -> str:
    return " ".join(str(value).casefold().replace("ё", "е").split())


def _dimension_key(label: str) -> str:
    return _norm(label).replace(" ", "_")


def _raw_footer_entries(scene_output: str) -> List[Tuple[str, List[Dict[str, Any]]]]:
    if not isinstance(scene_output, str) or "Отношения:" not in scene_output:
        return []
    lines = scene_output.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Отношения:") + 1
    except StopIteration:
        return []

    result: List[Tuple[str, List[Dict[str, Any]]]] = []
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Ход "):
            break
        if " - " not in line:
            continue
        name, raw_metrics = line.split(" - ", 1)
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
            result.append((name.strip(), metrics))
    return result


def _resolve_map_key(
    cards: Iterable[Dict[str, Any]],
    raw_key: Any,
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> str | None:
    text = str(raw_key or "").strip()
    if not text:
        return None
    if "->" in text:
        text = text.split("->", 1)[0].strip()
    return resolve_character_id(cards, text) or text


def _flat_relationships(
    relationships: Any,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(relationships, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_value in relationships.items():
        character_id = _resolve_map_key(cards, raw_key, resolve_character_id)
        if not character_id or not isinstance(raw_value, dict):
            continue
        bucket = result.setdefault(character_id, {})
        for key, value in raw_value.items():
            bucket[str(key)] = deepcopy(value)
    return result


def _dimensions_from_flat(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    result: List[Dict[str, Any]] = []
    for label, metric_value in value.items():
        if not _is_number(metric_value):
            continue
        result.append(
            {
                "key": _dimension_key(str(label)),
                "label": str(label),
                "value": metric_value,
            }
        )
    return result[:MAX_DIMENSIONS]


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


def _empty_relation(owner_id: str, pov_id: str, dimensions: List[Dict[str, Any]]) -> Dict[str, Any]:
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


def _normalise_documents(
    state: Dict[str, Any],
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Dict[str, Any]]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    raw_docs = state.get("relationship_documents")
    docs: Dict[str, Dict[str, Any]] = {}

    if isinstance(raw_docs, dict):
        for raw_owner, raw_doc in raw_docs.items():
            owner_id = _resolve_map_key(cards, raw_owner, resolve_character_id)
            if not owner_id or owner_id == pov_id or not isinstance(raw_doc, dict):
                continue
            relations = raw_doc.get("relations") if isinstance(raw_doc.get("relations"), list) else []
            normalised_relations: List[Dict[str, Any]] = []
            for relation in relations:
                if not isinstance(relation, dict):
                    continue
                target = resolve_character_id(cards, relation.get("target_character_id")) or str(
                    relation.get("target_character_id") or ""
                )
                if not target:
                    continue
                item = deepcopy(relation)
                item["target_character_id"] = target
                item["dimensions"] = _normalise_dimensions(item.get("dimensions"))
                item.setdefault("relationship_type", "установленная связь")
                item.setdefault("relationship_context", "")
                item.setdefault("current_dynamic", "")
                item.setdefault("beliefs_about_target", [])
                item.setdefault("unresolved_between_them", [])
                item.setdefault("dynamic_constraints", [])
                item.setdefault("change_reasons", [])
                item.setdefault("last_changed_turn", 0)
                normalised_relations.append(item)
            docs[owner_id] = {"owner_character_id": owner_id, "relations": normalised_relations}

    flat = _flat_relationships(
        state.get("relationships", {}), cards=cards, resolve_character_id=resolve_character_id
    )
    for owner_id, values in flat.items():
        if owner_id == pov_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = next(
            (item for item in doc["relations"] if str(item.get("target_character_id")) == pov_id),
            None,
        )
        if relation is None and pov_id:
            relation = _empty_relation(owner_id, pov_id, _dimensions_from_flat(values))
            doc["relations"].append(relation)
        elif relation is not None and not relation.get("dimensions"):
            relation["dimensions"] = _dimensions_from_flat(values)

    return docs


def _footer_by_character(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    for name, metrics in _raw_footer_entries(scene_output):
        character_id = resolve_character_id(cards, name)
        if character_id:
            result[str(character_id)] = metrics
    return result


def _latest_footer_values(
    turns: List[Dict[str, Any]],
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[Dict[str, Any]]]:
    latest: Dict[str, List[Dict[str, Any]]] = {}
    for turn in turns if isinstance(turns, list) else []:
        footer = _footer_by_character(
            turn.get("scene_output", "") if isinstance(turn, dict) else "",
            cards=cards,
            resolve_character_id=resolve_character_id,
        )
        latest.update(footer)
    return latest


def _sync_flat_from_documents(state: Dict[str, Any], docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    flat = result.get("relationships") if isinstance(result.get("relationships"), dict) else {}
    canonical_flat: Dict[str, Dict[str, Any]] = {}

    for owner_id, doc in docs.items():
        relation = next(
            (
                item
                for item in doc.get("relations", [])
                if isinstance(item, dict) and str(item.get("target_character_id")) == pov_id
            ),
            None,
        )
        if relation is None:
            continue
        metrics = {
            str(item.get("label") or item.get("key")): item.get("value")
            for item in _normalise_dimensions(relation.get("dimensions"))
        }
        old = flat.get(owner_id) if isinstance(flat.get(owner_id), dict) else {}
        metadata = {str(k): deepcopy(v) for k, v in old.items() if not _is_number(v)}
        canonical_flat[owner_id] = {**metadata, **metrics}

    for raw_key, value in flat.items():
        if raw_key not in canonical_flat and isinstance(value, dict):
            canonical_flat[str(raw_key)] = deepcopy(value)

    result["relationships"] = canonical_flat
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
    """Migrate/repair into the old-generator directed relationship document model.

    Durable saved state wins. History is used only if a relation disappeared completely, so
    sessions damaged by the recent schema-lock experiment can recover the last shown values.
    """
    result = deepcopy(state if isinstance(state, dict) else {})
    docs = _normalise_documents(result, cards=cards, resolve_character_id=resolve_character_id)
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    latest = _latest_footer_values(turns, cards=cards, resolve_character_id=resolve_character_id)
    for owner_id, metrics in latest.items():
        if owner_id == pov_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = next(
            (item for item in doc["relations"] if str(item.get("target_character_id")) == pov_id),
            None,
        )
        if relation is None and pov_id:
            relation = _empty_relation(owner_id, pov_id, [])
            doc["relations"].append(relation)
        if relation is not None and not relation.get("dimensions"):
            relation["dimensions"] = [
                {"key": item["key"], "label": item["label"], "value": item["value"]}
                for item in metrics[:MAX_DIMENSIONS]
            ]

    return _sync_flat_from_documents(result, docs)


def build_relationship_lens(
    state: Dict[str, Any],
    *,
    cards: Iterable[Dict[str, Any]],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    """Port of the old generator's relationship_lens, limited to NPC -> POV."""
    docs = _normalise_documents(state, cards=cards, resolve_character_id=resolve_character_id)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = list(dict.fromkeys(present_character_ids(state)))

    names: Dict[str, str] = {}
    for card in cards:
        cid = str(card.get("character_id") or card.get("id") or card.get("name") or "")
        if cid:
            identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
            names[cid] = str(card.get("name") or card.get("full_name") or identity.get("name") or cid)

    relations: List[Dict[str, Any]] = []
    for owner_id in present:
        if owner_id == pov_id:
            continue
        doc = docs.get(owner_id, {})
        for relation in doc.get("relations", []) if isinstance(doc, dict) else []:
            if not isinstance(relation, dict) or str(relation.get("target_character_id")) != pov_id:
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
            "Relationship dimensions are causal state, not decorative footer numbers. Before choosing an NPC reaction, line, initiative or interpretation, combine their actual dimensions with personality, goals, knowledge and current state. Absence of a dimension is not zero. Do not create 'interest' as a generic fallback merely because the footer needs a number. Preserve established dimensions across absences and later meetings; create or change only dimensions genuinely established by this relationship and scene. If a present NPC already has dimensions here, the visible Relationships footer MUST show those same dimensions and current values, plus only real scene deltas. Do not replace the set with freshly invented words on a reunion."
        ),
    }


def relationship_snapshot_for_present(
    state: Dict[str, Any],
    *,
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    relationships = state.get("relationships", {}) if isinstance(state.get("relationships"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    result: Dict[str, Any] = {}
    for character_id in present_character_ids(state):
        if character_id == pov_id:
            continue
        relation = relationships.get(character_id)
        if isinstance(relation, dict):
            metrics = {str(k): v for k, v in relation.items() if _is_number(v)}
            if metrics:
                result[character_id] = {"metrics": metrics}
    return result


def _merge_dimensions(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = _normalise_dimensions(existing)
    by_key = {_norm(item.get("key") or item.get("label")): index for index, item in enumerate(result)}
    by_label = {_norm(item.get("label")): index for index, item in enumerate(result)}
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip()
        value = raw.get("value")
        if not label or not _is_number(value):
            continue
        key = str(raw.get("key") or _dimension_key(label)).strip()
        identity = _norm(key)
        label_identity = _norm(label)
        index = by_key.get(identity)
        if index is None:
            index = by_label.get(label_identity)
        if index is None:
            if len(result) >= MAX_DIMENSIONS:
                continue
            result.append({"key": key, "label": label, "value": value})
            index = len(result) - 1
        else:
            # Value changes, established key/label do not silently rename.
            result[index]["value"] = value
        by_key[_norm(result[index].get("key"))] = index
        by_label[_norm(result[index].get("label"))] = index
    return result


def apply_relationship_updates(
    state: Dict[str, Any],
    updates: Any,
    *,
    cards: Iterable[Dict[str, Any]],
    turn_number: int,
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    if not isinstance(updates, list) or not updates:
        return deepcopy(state)
    result = deepcopy(state)
    docs = _normalise_documents(result, cards=cards, resolve_character_id=resolve_character_id)
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = resolve_character_id(cards, raw.get("owner_character_id") or raw.get("character_id"))
        target_id = resolve_character_id(cards, raw.get("target_character_id") or pov_id)
        if not owner_id or not target_id or owner_id == pov_id or target_id != pov_id:
            continue
        incoming_dimensions = _normalise_dimensions(raw.get("dimensions"))
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = next(
            (item for item in doc["relations"] if str(item.get("target_character_id")) == target_id),
            None,
        )
        if relation is None:
            relation = _empty_relation(owner_id, target_id, [])
            doc["relations"].append(relation)
        relation["dimensions"] = _merge_dimensions(relation.get("dimensions", []), incoming_dimensions)
        for field in (
            "relationship_type",
            "relationship_context",
            "current_dynamic",
            "beliefs_about_target",
            "unresolved_between_them",
            "dynamic_constraints",
        ):
            if field in raw:
                relation[field] = deepcopy(raw[field])
        reasons = raw.get("change_reasons")
        if isinstance(reasons, list) and reasons:
            existing_reasons = relation.get("change_reasons") if isinstance(relation.get("change_reasons"), list) else []
            relation["change_reasons"] = (existing_reasons + [str(x) for x in reasons if str(x).strip()])[-50:]
        if incoming_dimensions or any(field in raw for field in ("current_dynamic", "relationship_type")):
            relation["last_changed_turn"] = turn_number

    return _sync_flat_from_documents(result, docs)


def apply_footer_fallback(
    state: Dict[str, Any],
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    turn_number: int,
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    """Old-style footer fallback: merge changes, never replace the relationship document."""
    footer = _footer_by_character(scene_output, cards=cards, resolve_character_id=resolve_character_id)
    if not footer:
        return deepcopy(state)
    result = deepcopy(state)
    docs = _normalise_documents(result, cards=cards, resolve_character_id=resolve_character_id)
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(present_character_ids(result))

    for owner_id, metrics in footer.items():
        if owner_id == pov_id or owner_id not in present:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = next(
            (item for item in doc["relations"] if str(item.get("target_character_id")) == pov_id),
            None,
        )
        if relation is None:
            relation = _empty_relation(owner_id, pov_id, [])
            doc["relations"].append(relation)
        existing = _normalise_dimensions(relation.get("dimensions"))
        existing_labels = {_norm(item.get("label")) for item in existing}
        footer_labels = {_norm(item.get("label")) for item in metrics}

        # If an established relationship suddenly returns with a completely different
        # vocabulary, keep the durable relationship instead of replacing it. This is the
        # reunion bug that triggered the rollback to the old generator model.
        if existing and footer_labels and existing_labels.isdisjoint(footer_labels):
            continue

        incoming = [
            {"key": item["key"], "label": item["label"], "value": item["value"]}
            for item in metrics
        ]
        before = {item["key"]: item["value"] for item in existing}
        relation["dimensions"] = _merge_dimensions(existing, incoming)
        after = {item["key"]: item["value"] for item in relation["dimensions"]}
        if after != before:
            relation["last_changed_turn"] = turn_number

    return _sync_flat_from_documents(result, docs)


def relationship_patch_from_scene(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    state: Dict[str, Any],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    """Compatibility path used by current commit code.

    Unlike the short-lived schema-lock implementation, this never rejects a scene for a renamed
    or missing metric. It applies the old-generator merge semantics and returns only changed
    persistent relationship documents/maps.
    """
    updated = apply_footer_fallback(
        state,
        scene_output,
        cards=cards,
        turn_number=0,
        resolve_character_id=resolve_character_id,
        present_character_ids=present_character_ids,
    )
    patch: Dict[str, Any] = {}
    for key in ("relationships", "relationship_documents"):
        if updated.get(key) != state.get(key):
            patch[key] = deepcopy(updated.get(key, {}))
    return patch


def overwrite_relationship_snapshots(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility merge. Storage already received the same state_patch during commit."""
    result = deepcopy(state if isinstance(state, dict) else {})
    if isinstance(patch.get("relationships"), dict):
        relationships = result.get("relationships") if isinstance(result.get("relationships"), dict) else {}
        for character_id, relation in patch["relationships"].items():
            relationships[str(character_id)] = deepcopy(relation)
        result["relationships"] = relationships
    result.pop("relationship_schemas", None)
    return result
