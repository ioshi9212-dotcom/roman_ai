from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from . import relationship_runtime
from . import runtime_fixes as base
from . import runtime_fixes_compat as compat
from . import storage


MAX_RELATIONSHIP_DIMENSIONS = 32
_RELATIONSHIP_GROWTH_VERSION = 3

# New labels are intentionally small and stable. Existing legacy labels remain valid.
_FIXED_NEW_LABELS = {
    base._relationship_norm(label)
    for label in (
        "доверие",
        "близость",
        "привязанность",
        "симпатия",
        "влечение",
        "уважение",
        "подозрение",
        "настороженность",
        "раздражение",
        "обида",
        "ревность",
        "страх",
        "соперничество",
    )
}
_RELATIONSHIP_BLOCK = re.compile(
    r"(?ms)^Отношения:\s*\n.*?(?=^\s*Ход\s+\d+\s*·\s*цикл\b)"
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bounded_scene_delta(value: Any) -> float | None:
    if not _is_number(value):
        return None
    number = float(value)
    # Canonical validation happens in relationship_policy_runtime and may permit larger
    # time-skip or critical-event deltas. Keep only a hard corruption guard here.
    if abs(number) > 30:
        return None
    return number


def _clamp_relationship_value(value: Any) -> float:
    return max(0.0, min(100.0, float(value)))


def _strip_relationship_footer(scene_output: str) -> str:
    """Keep the visible scene untouched for the player, but remove its relationship rows internally.

    The footer is UI. Numeric canon is applied from relationship_updates (or a bounded legacy
    footer-delta fallback) against the authoritative saved baseline.
    """
    text = str(scene_output or "")
    return _RELATIONSHIP_BLOCK.sub("Отношения:\n\n", text)


def _existing_label_norms(state: Dict[str, Any], owner_id: str) -> set[str]:
    return {
        base._relationship_norm(label)
        for label in base._numeric_relationships(state, owner_id)
    }


def _merge_footer_delta_fallbacks(
    payload: Dict[str, Any],
    *,
    root,
) -> Dict[str, Any]:
    """Initialize only a character's first relationship baseline from the visible footer.

    Once any numeric relationship metric exists for that NPC, the footer is display-only and can
    never mutate or append canon. Existing relationships change only through causal relationship_updates.
    """
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        return result

    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    footer = compat._parse_footer_compat(
        str(result.get("scene_output") or ""),
        cards=cards,
        resolve_character_id=base._resolve_character_id,
    )
    if not footer:
        return result

    rows = deepcopy(extracted.get("relationship_updates"))
    if not isinstance(rows, list):
        rows = []
    explicit_owners = {
        str(base._resolve_character_id(cards, raw.get("character_id")) or raw.get("character_id") or "")
        for raw in rows
        if isinstance(raw, dict)
    }

    for owner_id, dimensions in footer.items():
        owner_id = str(owner_id)
        if not owner_id or owner_id in explicit_owners or _existing_label_norms(state, owner_id):
            continue
        baseline: List[Dict[str, Any]] = []
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            label = str(dim.get("label") or dim.get("key") or "").strip()
            value = dim.get("value")
            if (
                not label
                or not _is_number(value)
            ):
                continue
            baseline.append({"label": label, "value": value})
        if baseline:
            rows.append({"character_id": owner_id, "dimensions": baseline})

    extracted = deepcopy(extracted)
    extracted["relationship_updates"] = rows
    result["extracted"] = extracted
    return result

def _merge_footer_dimensions(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = relationship_runtime._normalise_dimensions(existing)
    by_label = {relationship_runtime._norm(item["label"]): index for index, item in enumerate(result)}
    for item in incoming:
        label = str(item.get("label") or item.get("key") or "").strip()
        value = item.get("value")
        if not label or not relationship_runtime._is_number(value):
            continue
        normalized = relationship_runtime._norm(label)
        if normalized in by_label:
            result[by_label[normalized]]["value"] = value
        elif len(result) < MAX_RELATIONSHIP_DIMENSIONS:
            result.append({"key": str(item.get("key") or relationship_runtime._dimension_key(label)), "label": label, "value": value})
            by_label[normalized] = len(result) - 1
    return result


def _validate_dimensions(incoming: List[Dict[str, Any]], baseline: Dict[str, int | float], *, owner_name: str = "NPC") -> None:
    # The visible footer is display-only. Canonical relationship writes are reconciled separately.
    return None


def _validate_visible_footer(
    scene_output: str,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    # Parse for compatibility/diagnostics, but never brick a gameplay transaction over footer drift.
    return compat._parse_footer_compat(
        scene_output,
        cards=cards,
        resolve_character_id=base._resolve_character_id,
    )


def _hidden_relationship_scene(
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
    start_present: set[str],
    upsert_ids: set[str],
) -> str:
    """Turn causal relationship_updates into authoritative final values.

    Existing metrics move only by a small explicit delta computed from the saved baseline. A supplied
    absolute value cannot roll an established metric backward or forward by itself. New metrics may
    initialize from their supplied value. The same mechanism works whether the NPC stays or leaves.
    """
    if updates in (None, []):
        return ""
    if not isinstance(updates, list):
        base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "relationship_updates must be an array.")

    # Participation is validated by relationship_policy_runtime using concrete current-turn evidence,
    # including physical presence/transition and real remote dialogue/contact.
    lines: List[str] = []

    for raw in updates:
        if not isinstance(raw, dict):
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)

        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            # Metadata-only rows are split out by living_world_runtime. Be tolerant if one reaches here.
            continue

        baseline = base._numeric_relationships(state_before, owner_id)
        baseline_by_norm = {
            base._relationship_norm(label): (str(label), float(value))
            for label, value in baseline.items()
            if _is_number(value)
        }

        rendered: List[str] = []
        seen: set[str] = set()
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("key") or "").strip()
            normalized = base._relationship_norm(label)
            if not label or not normalized or normalized in seen:
                continue
            seen.add(normalized)

            if normalized in baseline_by_norm:
                saved_label, old_value = baseline_by_norm[normalized]
                delta = _bounded_scene_delta(item.get("delta"))
                # No delta means "snapshot/display", not permission to replace canonical history.
                final_value = old_value if delta is None else _clamp_relationship_value(old_value + delta)
                label = saved_label
            else:
                value = item.get("value")
                if not _is_number(value):
                    continue
                final_value = _clamp_relationship_value(value)

            rendered.append(f"{label} {final_value:g}")

        if rendered:
            lines.append(f"{owner_id} - {'; '.join(rendered)}")

    return "Отношения:\n" + "\n".join(lines) if lines else ""
