from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from . import relationship_runtime
from . import runtime_fixes as base
from . import runtime_fixes_compat as compat
from . import storage


MAX_RELATIONSHIP_DIMENSIONS = 12
_RELATIONSHIP_GROWTH_VERSION = 3
_ORIGINAL_PREPARE_EXTRACTED_FOR_COMMIT = base._prepare_extracted_for_commit

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
    # Scene builder allows small per-turn movement. An absurd old/stale footer delta is ignored,
    # not turned into a destructive canonical jump and not rejected with a 409.
    if abs(number) > 3:
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
    """Backward-compatible bridge for GPTs still treating the footer as the numeric update channel.

    Only a bounded delta can become a fallback update. The displayed absolute number is never trusted
    for an established metric. Explicit relationship_updates with a real delta win.
    """
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        return result

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
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

    by_owner: Dict[str, Dict[str, Any]] = {}
    explicit_delta_labels: Dict[str, set[str]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        owner_id = base._resolve_character_id(cards, raw.get("character_id")) or str(raw.get("character_id") or "")
        if not owner_id:
            continue
        by_owner.setdefault(str(owner_id), raw)
        for dim in raw.get("dimensions", []) if isinstance(raw.get("dimensions"), list) else []:
            if not isinstance(dim, dict) or _bounded_scene_delta(dim.get("delta")) is None:
                continue
            label = base._relationship_norm(str(dim.get("label") or dim.get("key") or ""))
            if label:
                explicit_delta_labels.setdefault(str(owner_id), set()).add(label)

    for owner_id, dimensions in footer.items():
        owner_id = str(owner_id)
        existing = _existing_label_norms(state, owner_id)
        fallback: List[Dict[str, Any]] = []
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            label_text = str(dim.get("label") or dim.get("key") or "").strip()
            label_norm = base._relationship_norm(label_text)
            if not label_norm:
                continue
            if label_norm in explicit_delta_labels.get(owner_id, set()):
                continue
            # Legacy saved labels remain valid; genuinely new labels must come from the fixed vocabulary.
            if label_norm not in existing and label_norm not in _FIXED_NEW_LABELS:
                continue
            raw_delta = dim.get("delta")
            if not _is_number(raw_delta):
                continue
            # Existing counters may move only by the scene-sized delta. A newly created counter uses
            # its supplied initial value; its printed delta is only a backwards-compatible change marker.
            delta = _bounded_scene_delta(raw_delta) if label_norm in existing else float(raw_delta)
            if delta is None:
                continue
            value = dim.get("value")
            if not _is_number(value):
                continue
            fallback.append({"label": label_text, "value": value, "delta": delta})

        if not fallback:
            continue
        target = by_owner.get(owner_id)
        if target is None:
            target = {"character_id": owner_id, "dimensions": []}
            rows.append(target)
            by_owner[owner_id] = target
        dims = target.get("dimensions")
        if not isinstance(dims, list):
            dims = []
            target["dimensions"] = dims
        for item in fallback:
            label_norm = base._relationship_norm(str(item.get("label") or ""))
            replaced = False
            for index, existing_item in enumerate(dims):
                if not isinstance(existing_item, dict):
                    continue
                existing_norm = base._relationship_norm(
                    str(existing_item.get("label") or existing_item.get("key") or "")
                )
                if existing_norm != label_norm:
                    continue
                # A real explicit delta already won above. A snapshot-only duplicate must not
                # suppress the safe footer-delta fallback during migration.
                if _bounded_scene_delta(existing_item.get("delta")) is None:
                    dims[index] = item
                replaced = True
                break
            if not replaced:
                dims.append(item)

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

    final_present = set(str(value) for value in compat._current_present_ids(state_after))
    allowed = set(str(value) for value in start_present) | final_present | set(str(value) for value in upsert_ids)
    lines: List[str] = []

    for raw in updates:
        if not isinstance(raw, dict):
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)
        if owner_id not in allowed:
            owner_name = compat._card_display_name(cards, owner_id)
            base._http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                f"{owner_name}: relationship update is allowed only for an NPC who participated in this turn.",
            )

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


def _prepare_extracted_for_commit(
    payload: Dict[str, Any],
    *,
    root,
    turn_number: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    # Preserve old GPT installations: a valid small footer delta becomes a relationship_update fallback.
    prepared_payload = _merge_footer_delta_fallbacks(payload, root=root)
    # Never let the visible footer itself overwrite persistent numeric canon.
    prepared_payload["scene_output"] = _strip_relationship_footer(str(prepared_payload.get("scene_output") or ""))
    return _ORIGINAL_PREPARE_EXTRACTED_FOR_COMMIT(
        prepared_payload,
        root=root,
        turn_number=turn_number,
    )


def _install_packet_policy_wrapper() -> None:
    previous = base._rewrite_turn_packet

    def rewrite(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        result = previous(session_id, manifest)
        root = storage.SESSIONS_DIR / session_id
        packet = storage._read_json(root / "turn_packet.json", {})
        raw = "".join(packet.get("chunks", []))
        if not raw:
            return result
        context = json.loads(raw)

        lens = context.get("relationship_lens") if isinstance(context.get("relationship_lens"), dict) else {}
        lens["initialization_required"] = False
        lens["initialization_instruction"] = (
            "Do not invent a relationship just because an NPC is present. When a meaningful directed attitude exists, "
            "initialize only relevant dimensions. Existing dimensions persist independently of scene presence."
        )
        context["relationship_lens"] = lens
        context["relationship_lens_instruction"] = (
            "MANDATORY NPC->POV state. Treat the saved relationship_lens snapshot as the authoritative start of this turn. "
            "Old relationship numbers inside prior scene text are history/display only and must never replace this snapshot."
        )

        policy = context.get("relationship_policy") if isinstance(context.get("relationship_policy"), dict) else {}
        policy.update({
            "source_of_truth": "persistent relationship state + causal relationship_updates",
            "footer_is_display_only": True,
            "footer_is_transaction_gate": False,
            "footer_required_for_every_present_npc": False,
            "fresh_baseline_required": False,
            "zero_dimensions_may_be_hidden": True,
            "new_dimensions_may_be_appended": True,
            "existing_metric_update": (
                "If an established metric truly changes, send it in extracted.relationship_updates with delta. "
                "The server computes saved_value + delta; the supplied absolute value is not authoritative."
            ),
            "unchanged_metric_rule": (
                "If a metric did not change, do not send a numeric update. Its saved value persists automatically."
            ),
            "presence_rule": (
                "The same relationship update works whether the NPC remains present or leaves during this turn."
            ),
            "footer_validation": (
                "The footer only displays the relationship state for the reader. Footer omissions, stale absolute values "
                "or presence drift do not overwrite persistent relationship canon and do not block the turn."
            ),
            "instruction": (
                "Start from authoritative_start_snapshot/relationship_lens. Persist only real numeric changes through "
                "relationship_updates. For an established metric include a small causal delta; never copy an older scene's "
                "absolute number back into canon. The footer is display, not storage."
            ),
        })
        context["relationship_policy"] = policy

        persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
        persistence["relationship_numeric_updates"] = {
            "optional": True,
            "when": "Only when an NPC->POV numeric relationship metric actually changed in this turn.",
            "format": '[{"character_id":"npc_id","dimensions":[{"label":"доверие","value":12,"delta":2}]}]',
            "instruction": (
                "For an established metric, delta is the causal write and Railway computes it from the saved baseline. "
                "Do not resend unchanged dimensions. Works for present and departed participating NPCs."
            ),
        }
        context["persistence_contract"] = persistence

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        packet["chunks"] = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
        packet["chunk_count"] = len(packet["chunks"])
        packet["read_chunks"] = []
        packet["relationship_growth_runtime"] = _RELATIONSHIP_GROWTH_VERSION
        storage._write_json(root / "turn_packet.json", packet)
        updated = dict(result)
        updated["chunk_count"] = packet["chunk_count"]
        return updated

    base._rewrite_turn_packet = rewrite


def install() -> None:
    relationship_runtime.MAX_DIMENSIONS = MAX_RELATIONSHIP_DIMENSIONS
    relationship_runtime._merge_footer_dimensions = _merge_footer_dimensions
    compat._validate_dimensions = _validate_dimensions
    compat._validate_visible_footer = _validate_visible_footer
    compat._hidden_relationship_scene = _hidden_relationship_scene
    base._validate_dimensions = _validate_dimensions
    base._validate_visible_footer = _validate_visible_footer
    base._hidden_relationship_scene = _hidden_relationship_scene
    base._prepare_extracted_for_commit = _prepare_extracted_for_commit
    _install_packet_policy_wrapper()
