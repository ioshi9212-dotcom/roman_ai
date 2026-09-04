from __future__ import annotations

from typing import Any, Dict, List

from . import relationship_runtime
from . import runtime_fixes as base
from . import runtime_fixes_compat as compat


MAX_RELATIONSHIP_DIMENSIONS = 12


def _merge_footer_dimensions(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Preserve established dimensions while allowing genuinely new ones to be added."""
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
            continue
        if len(result) >= MAX_RELATIONSHIP_DIMENSIONS:
            continue
        result.append(
            {
                "key": str(item.get("key") or relationship_runtime._dimension_key(label)),
                "label": label,
                "value": value,
            }
        )
        by_label[normalized] = len(result) - 1
    return result


def _validate_dimensions(
    incoming: List[Dict[str, Any]],
    baseline: Dict[str, int | float],
    *,
    owner_name: str = "NPC",
) -> None:
    """Validate shown dimensions without forcing zero-valued dimensions into the footer."""
    baseline_by_norm = {
        base._relationship_norm(label): (label, value) for label, value in baseline.items()
    }
    incoming_by_norm: Dict[str, Dict[str, Any]] = {}
    for item in incoming:
        label = str(item.get("label") or "").strip()
        normalized = base._relationship_norm(label)
        if not normalized:
            continue
        if normalized in incoming_by_norm:
            base._http_error(
                409,
                "RELATIONSHIP_DIMENSION_DUPLICATE",
                f"{owner_name}: relationship dimension {label!r} is duplicated in the footer.",
            )
        incoming_by_norm[normalized] = item

    missing_nonzero = {
        key for key, (_label, value) in baseline_by_norm.items() if value != 0
    } - set(incoming_by_norm)
    if missing_nonzero:
        labels = ", ".join(baseline_by_norm[key][0] for key in sorted(missing_nonzero))
        base._http_error(
            409,
            "RELATIONSHIP_DIMENSIONS_INCOMPLETE",
            f"{owner_name}: footer omitted saved non-zero dimensions: {labels}.",
        )

    for normalized, item in incoming_by_norm.items():
        saved = baseline_by_norm.get(normalized)
        if not saved:
            continue
        saved_label, old_value = saved
        delta = item.get("delta")
        if delta is None:
            continue
        expected = old_value + delta
        final_value = item.get("value")
        if abs(float(final_value) - float(expected)) > 1e-9:
            base._http_error(
                409,
                "RELATIONSHIP_ARITHMETIC_MISMATCH",
                f"{owner_name}: {saved_label} started at {old_value}, displayed delta is {delta:+g}, so final value must be {expected}, not {final_value}.",
            )


def _validate_visible_footer(
    scene_output: str,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    footer = compat._parse_footer_compat(
        scene_output,
        cards=cards,
        resolve_character_id=base._resolve_character_id,
    )
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    final_present = set(compat._current_present_ids(state_after))

    for owner_id in footer:
        owner_name = compat._card_display_name(cards, owner_id)
        if owner_id == pov_id:
            base._http_error(
                409,
                "RELATIONSHIP_DIRECTION_INVALID",
                f"{owner_name}: Relationships footer may contain NPC -> POV only, never POV -> NPC.",
            )
        if owner_id not in final_present:
            base._http_error(
                409,
                "RELATIONSHIP_FOOTER_ABSENT_NPC",
                f"{owner_name}: relationship row is visible although this NPC is absent at scene end.",
            )

    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        owner_name = compat._card_display_name(cards, owner_id)
        baseline = base._numeric_relationships(state_before, owner_id)
        incoming = footer.get(owner_id, [])
        nonzero_baseline = {label: value for label, value in baseline.items() if value != 0}

        # A character with no meaningful numeric relationship yet does not need a decorative
        # zero row. The relationship can remain absent until the story naturally creates one.
        if not incoming:
            if nonzero_baseline:
                labels = ", ".join(nonzero_baseline)
                base._http_error(
                    409,
                    "RELATIONSHIP_FOOTER_REQUIRED",
                    f"{owner_name}: saved non-zero relationship dimensions must remain visible: {labels}.",
                )
            continue

        if baseline:
            _validate_dimensions(incoming, baseline, owner_name=owner_name)
        elif not 1 <= len(incoming) <= 3:
            base._http_error(
                409,
                "RELATIONSHIP_BASELINE_INVALID",
                f"{owner_name}: first meaningful relationship baseline must contain 1-3 natural dimensions; received {len(incoming)}.",
            )
    return footer


def install() -> None:
    relationship_runtime.MAX_DIMENSIONS = MAX_RELATIONSHIP_DIMENSIONS
    relationship_runtime._merge_footer_dimensions = _merge_footer_dimensions
    compat._validate_dimensions = _validate_dimensions
    compat._validate_visible_footer = _validate_visible_footer
    base._validate_dimensions = _validate_dimensions
    base._validate_visible_footer = _validate_visible_footer
