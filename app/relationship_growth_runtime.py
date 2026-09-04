from __future__ import annotations

import json
from typing import Any, Dict, List

from . import relationship_runtime
from . import runtime_fixes as base
from . import runtime_fixes_compat as compat
from . import storage


MAX_RELATIONSHIP_DIMENSIONS = 12


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
    baseline_by_norm = {base._relationship_norm(label): (label, value) for label, value in baseline.items()}
    incoming_by_norm: Dict[str, Dict[str, Any]] = {}
    for item in incoming:
        label = str(item.get("label") or "").strip()
        normalized = base._relationship_norm(label)
        if not normalized:
            continue
        if normalized in incoming_by_norm:
            base._http_error(409, "RELATIONSHIP_DIMENSION_DUPLICATE", f"{owner_name}: relationship dimension {label!r} is duplicated in the footer.")
        incoming_by_norm[normalized] = item
    missing_nonzero = {key for key, (_label, value) in baseline_by_norm.items() if value != 0} - set(incoming_by_norm)
    if missing_nonzero:
        labels = ", ".join(baseline_by_norm[key][0] for key in sorted(missing_nonzero))
        base._http_error(409, "RELATIONSHIP_DIMENSIONS_INCOMPLETE", f"{owner_name}: footer omitted saved non-zero dimensions: {labels}.")
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
            base._http_error(409, "RELATIONSHIP_ARITHMETIC_MISMATCH", f"{owner_name}: {saved_label} started at {old_value}, displayed delta is {delta:+g}, so final value must be {expected}, not {final_value}.")


def _validate_visible_footer(scene_output: str, *, cards: List[Dict[str, Any]], state_before: Dict[str, Any], state_after: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    footer = compat._parse_footer_compat(scene_output, cards=cards, resolve_character_id=base._resolve_character_id)
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    final_present = set(compat._current_present_ids(state_after))
    for owner_id in footer:
        owner_name = compat._card_display_name(cards, owner_id)
        if owner_id == pov_id:
            base._http_error(409, "RELATIONSHIP_DIRECTION_INVALID", f"{owner_name}: Relationships footer may contain NPC -> POV only, never POV -> NPC.")
        if owner_id not in final_present:
            base._http_error(409, "RELATIONSHIP_FOOTER_ABSENT_NPC", f"{owner_name}: relationship row is visible although this NPC is absent at scene end.")
    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        owner_name = compat._card_display_name(cards, owner_id)
        baseline = base._numeric_relationships(state_before, owner_id)
        incoming = footer.get(owner_id, [])
        nonzero_baseline = {label: value for label, value in baseline.items() if value != 0}
        if not incoming:
            if nonzero_baseline:
                labels = ", ".join(nonzero_baseline)
                base._http_error(409, "RELATIONSHIP_FOOTER_REQUIRED", f"{owner_name}: saved non-zero relationship dimensions must remain visible: {labels}.")
            continue
        if baseline:
            _validate_dimensions(incoming, baseline, owner_name=owner_name)
        elif not 1 <= len(incoming) <= 3:
            base._http_error(409, "RELATIONSHIP_BASELINE_INVALID", f"{owner_name}: first meaningful relationship baseline must contain 1-3 natural dimensions; received {len(incoming)}.")
    return footer


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
            "Do not invent a baseline merely because an NPC is present. When the story creates a meaningful directed attitude, initialize 1-3 natural dimensions. "
            "Existing dimensions persist, and genuinely new dimensions may be appended later when causally established. Zero-valued dimensions may stay stored but be omitted from the footer."
        )
        context["relationship_lens"] = lens
        context["relationship_lens_instruction"] = (
            "MANDATORY causal NPC->POV relationship state. Initial dimensions are not a locked schema. Preserve established non-zero dimensions and naturally append new ones when the story creates them. "
            "Zero-valued dimensions may be hidden; an NPC with no meaningful relationship yet needs no decorative zero row."
        )
        policy = context.get("relationship_policy") if isinstance(context.get("relationship_policy"), dict) else {}
        policy["footer_required_for_every_present_npc"] = False
        policy["fresh_baseline_required"] = False
        policy["zero_dimensions_may_be_hidden"] = True
        policy["new_dimensions_may_be_appended"] = True
        policy["footer_validation"] = "Server-enforced: visible rows are NPC->POV and only for NPCs present at scene end. Saved non-zero dimensions stay visible. Zero dimensions may be omitted. New dimensions may be appended when causally established."
        policy["instruction"] = "Relationships are persistent but dynamically extensible. Preserve old non-zero dimensions, append genuinely new states when the plot creates them, hide zero values if desired, and never manufacture metrics just to keep numbers moving."
        context["relationship_policy"] = policy
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        packet["chunks"] = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
        packet["chunk_count"] = len(packet["chunks"])
        packet["read_chunks"] = []
        packet["relationship_growth_runtime"] = 1
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
    base._validate_dimensions = _validate_dimensions
    base._validate_visible_footer = _validate_visible_footer
    _install_packet_policy_wrapper()
