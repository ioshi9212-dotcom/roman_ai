from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Tuple


_METRIC_RE = re.compile(r"^(.+?)\s+(-?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)$")


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() else value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _metric_key(value: Any) -> str:
    return " ".join(str(value).casefold().replace("ё", "е").split())


def _raw_footer_entries(scene_output: str) -> List[Tuple[str, List[Tuple[str, int | float, int | float]]]]:
    if not isinstance(scene_output, str) or "Отношения:" not in scene_output:
        return []
    lines = scene_output.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Отношения:") + 1
    except StopIteration:
        return []

    result: List[Tuple[str, List[Tuple[str, int | float, int | float]]]] = []
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Ход "):
            break
        if " - " not in line:
            continue
        name, raw_metrics = line.split(" - ", 1)
        metrics: List[Tuple[str, int | float, int | float]] = []
        for raw_metric in raw_metrics.split(";"):
            match = _METRIC_RE.match(raw_metric.strip())
            if not match:
                continue
            label = match.group(1).strip()
            if label:
                metrics.append((label, _number(match.group(2)), _number(match.group(3))))
        if metrics:
            result.append((name.strip(), metrics))
    return result


def _canonical_relationship_map(
    relationships: Any,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    if not isinstance(relationships, dict):
        return {}
    result: Dict[str, Any] = {}
    for raw_key, value in relationships.items():
        text = str(raw_key)
        base = text.split("->", 1)[0].strip() if "->" in text else text.strip()
        character_id = resolve_character_id(cards, base) or base
        if not character_id:
            continue
        if character_id in result and isinstance(result[character_id], dict) and isinstance(value, dict):
            merged = deepcopy(result[character_id])
            merged.update(deepcopy(value))
            result[character_id] = merged
        else:
            result[character_id] = deepcopy(value)
    return result


def _numeric_metrics(value: Any) -> Dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items() if _is_number(val)}


def _schema_for_relation(value: Any) -> List[str]:
    return list(_numeric_metrics(value).keys())


def _find_relation(relationships: Dict[str, Any], character_id: str) -> Any:
    return relationships.get(character_id) or relationships.get(f"{character_id}->pov")


def _footer_by_character(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, List[Tuple[str, int | float, int | float]]]:
    result: Dict[str, List[Tuple[str, int | float, int | float]]] = {}
    for name, metrics in _raw_footer_entries(scene_output):
        character_id = resolve_character_id(cards, name)
        if character_id:
            result[str(character_id)] = metrics
    return result


def repair_relationship_state(
    state: Dict[str, Any],
    *,
    source: Dict[str, Any],
    turns: List[Dict[str, Any]],
    cards: Iterable[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    """Canonicalize relationship keys and reconstruct the stable metric schema/value chain.

    Existing sessions may contain duplicate name-vs-id keys or stray metrics created by an
    older footer bug. The first established schema is sticky. Later footer lines with renamed
    metrics or broken value/delta arithmetic are ignored when reconstructing the durable state.
    """
    result = deepcopy(state if isinstance(state, dict) else {})
    relationships = _canonical_relationship_map(
        result.get("relationships", {}), cards=cards, resolve_character_id=resolve_character_id
    )
    raw_schemas = _canonical_relationship_map(
        result.get("relationship_schemas", {}), cards=cards, resolve_character_id=resolve_character_id
    )

    starting = source.get("starting_state") if isinstance(source.get("starting_state"), dict) else {}
    starting_relationships = _canonical_relationship_map(
        starting.get("relationships", {}), cards=cards, resolve_character_id=resolve_character_id
    )

    schemas: Dict[str, List[str]] = {}
    reconstructed: Dict[str, Dict[str, int | float]] = {}

    all_ids = set(relationships) | set(starting_relationships) | set(raw_schemas)
    turn_footers: List[Dict[str, List[Tuple[str, int | float, int | float]]]] = []
    for turn in turns if isinstance(turns, list) else []:
        footer = _footer_by_character(
            turn.get("scene_output", "") if isinstance(turn, dict) else "",
            cards=cards,
            resolve_character_id=resolve_character_id,
        )
        turn_footers.append(footer)
        all_ids.update(footer)

    for character_id in all_ids:
        existing_schema = raw_schemas.get(character_id)
        if isinstance(existing_schema, list) and all(isinstance(x, str) and x.strip() for x in existing_schema):
            schema = [str(x).strip() for x in existing_schema]
        else:
            schema = _schema_for_relation(starting_relationships.get(character_id))
            if not schema:
                for footer in turn_footers:
                    if character_id in footer:
                        schema = [label for label, _, _ in footer[character_id]]
                        break
            if not schema:
                schema = _schema_for_relation(relationships.get(character_id))
        if not schema:
            continue

        schema_by_key = {_metric_key(label): label for label in schema}
        if len(schema_by_key) != len(schema):
            continue
        schemas[character_id] = list(schema_by_key.values())

        base = _numeric_metrics(starting_relationships.get(character_id))
        base_by_key = {_metric_key(label): value for label, value in base.items()}
        current: Dict[str, int | float] = {}
        if set(base_by_key) == set(schema_by_key):
            current = {schema_by_key[key]: base_by_key[key] for key in schema_by_key}

        for footer in turn_footers:
            metrics = footer.get(character_id)
            if not metrics:
                continue
            footer_by_key = {_metric_key(label): (value, delta) for label, value, delta in metrics}
            if set(footer_by_key) != set(schema_by_key):
                continue
            if not current:
                current = {schema_by_key[key]: footer_by_key[key][0] for key in schema_by_key}
                continue
            valid = True
            for key, canonical_label in schema_by_key.items():
                previous = current[canonical_label]
                value, delta = footer_by_key[key]
                if abs(float(previous) + float(delta) - float(value)) > 1e-9:
                    valid = False
                    break
            if valid:
                current = {schema_by_key[key]: footer_by_key[key][0] for key in schema_by_key}

        if not current:
            existing = _numeric_metrics(relationships.get(character_id))
            existing_by_key = {_metric_key(label): value for label, value in existing.items()}
            if set(schema_by_key).issubset(existing_by_key):
                current = {schema_by_key[key]: existing_by_key[key] for key in schema_by_key}

        old_relation = relationships.get(character_id)
        metadata = {
            str(key): deepcopy(value)
            for key, value in old_relation.items()
            if isinstance(old_relation, dict) and not _is_number(value)
        } if isinstance(old_relation, dict) else {}
        if current:
            relationships[character_id] = {**metadata, **current}
        elif metadata:
            relationships[character_id] = metadata

    result["relationships"] = relationships
    result["relationship_schemas"] = schemas
    return result


def relationship_snapshot_for_present(
    state: Dict[str, Any],
    *,
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    relationships = state.get("relationships", {}) if isinstance(state.get("relationships"), dict) else {}
    schemas = state.get("relationship_schemas", {}) if isinstance(state.get("relationship_schemas"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    result: Dict[str, Any] = {}
    for character_id in present_character_ids(state):
        if character_id == pov_id:
            continue
        relation = relationships.get(character_id)
        if isinstance(relation, dict):
            result[character_id] = {
                "metrics": _numeric_metrics(relation),
                "schema": deepcopy(schemas.get(character_id, [])),
            }
    return result


def relationship_patch_from_scene(
    scene_output: str,
    *,
    cards: Iterable[Dict[str, Any]],
    state: Dict[str, Any],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], list[str]],
) -> Dict[str, Any]:
    """Validate and persist the authoritative end-of-turn footer snapshot.

    Once metric names exist for NPC -> POV they are immutable unless repaired explicitly.
    For an established metric, displayed final value MUST equal previous saved value + delta.
    """
    footer = _footer_by_character(scene_output, cards=cards, resolve_character_id=resolve_character_id)
    if not footer:
        return {}

    present = set(present_character_ids(state))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    relationships = state.get("relationships", {}) if isinstance(state.get("relationships"), dict) else {}
    schemas = state.get("relationship_schemas", {}) if isinstance(state.get("relationship_schemas"), dict) else {}

    patch_relationships: Dict[str, Dict[str, int | float]] = {}
    patch_schemas: Dict[str, List[str]] = {}

    for character_id, metrics in footer.items():
        if character_id == pov_id or character_id not in present:
            continue

        previous = _numeric_metrics(_find_relation(relationships, character_id))
        schema = schemas.get(character_id)
        if not isinstance(schema, list) or not schema:
            schema = list(previous.keys()) if previous else [label for label, _, _ in metrics]
        schema = [str(label).strip() for label in schema if str(label).strip()]
        schema_by_key = {_metric_key(label): label for label in schema}
        footer_by_key = {_metric_key(label): (label, value, delta) for label, value, delta in metrics}

        if set(footer_by_key) != set(schema_by_key):
            raise RuntimeError(f"RELATIONSHIP_SCHEMA_MISMATCH:{character_id}")

        previous_by_key = {_metric_key(label): value for label, value in previous.items()}
        final_metrics: Dict[str, int | float] = {}
        for key, canonical_label in schema_by_key.items():
            _, value, delta = footer_by_key[key]
            if key in previous_by_key:
                expected = float(previous_by_key[key]) + float(delta)
                if abs(expected - float(value)) > 1e-9:
                    raise RuntimeError(f"RELATIONSHIP_ARITHMETIC_MISMATCH:{character_id}:{canonical_label}")
            final_metrics[canonical_label] = value

        patch_relationships[character_id] = final_metrics
        patch_schemas[character_id] = list(schema_by_key.values())

    patch: Dict[str, Any] = {}
    if patch_relationships:
        patch["relationships"] = patch_relationships
    if patch_schemas:
        patch["relationship_schemas"] = patch_schemas
    return patch


def overwrite_relationship_snapshots(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Apply validated relationship snapshots as replacement, pruning stale numeric metrics."""
    result = deepcopy(state if isinstance(state, dict) else {})
    relationships = result.get("relationships") if isinstance(result.get("relationships"), dict) else {}
    schemas = result.get("relationship_schemas") if isinstance(result.get("relationship_schemas"), dict) else {}
    patch_relationships = patch.get("relationships") if isinstance(patch.get("relationships"), dict) else {}
    patch_schemas = patch.get("relationship_schemas") if isinstance(patch.get("relationship_schemas"), dict) else {}

    for character_id, metrics in patch_relationships.items():
        old = relationships.get(character_id)
        metadata = {
            str(key): deepcopy(value)
            for key, value in old.items()
            if isinstance(old, dict) and not _is_number(value)
        } if isinstance(old, dict) else {}
        relationships[character_id] = {**metadata, **deepcopy(metrics)}
    for character_id, schema in patch_schemas.items():
        schemas[character_id] = deepcopy(schema)

    result["relationships"] = relationships
    result["relationship_schemas"] = schemas
    return result
