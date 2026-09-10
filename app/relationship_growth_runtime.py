from __future__ import annotations

import json
from typing import Any, Dict, List

from . import relationship_runtime
from . import runtime_fixes as base
from . import runtime_fixes_compat as compat
from . import storage


MAX_RELATIONSHIP_DIMENSIONS = 12
_RELATIONSHIP_GROWTH_VERSION = 2


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
    """Allow partial display rows while rejecting a complete metric-schema swap.

    Existing values are preserved by _merge_footer_dimensions, so omitted labels are not data loss.
    Deltas are explanatory UI and do not independently block a turn. A footer that replaces every
    established label with unrelated labels is still rejected so legacy relationship vocabularies
    cannot silently disappear under a new schema.
    """
    baseline_norms = {
        base._relationship_norm(label)
        for label, value in baseline.items()
        if relationship_runtime._is_number(value)
    }
    incoming_norms = {
        base._relationship_norm(str(item.get("label") or item.get("key") or ""))
        for item in incoming
        if isinstance(item, dict) and str(item.get("label") or item.get("key") or "").strip()
    }
    if baseline_norms and incoming_norms and baseline_norms.isdisjoint(incoming_norms):
        base._http_error(
            409,
            "RELATIONSHIP_DIMENSIONS_INCOMPLETE",
            f"{owner_name}: footer replaced all established relationship labels instead of preserving the existing relationship model.",
        )


def _validate_visible_footer(scene_output: str, *, cards: List[Dict[str, Any]], state_before: Dict[str, Any], state_after: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Reconcile footer drift instead of using display formatting as a hard transaction gate."""
    footer = compat._parse_footer_compat(scene_output, cards=cards, resolve_character_id=base._resolve_character_id)
    final_present = set(compat._current_present_ids(state_after))
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        incoming = footer.get(owner_id)
        baseline = base._numeric_relationships(state_before, owner_id)
        if incoming and baseline:
            _validate_dimensions(
                incoming,
                baseline,
                owner_name=compat._card_display_name(cards, owner_id),
            )
    return footer


def _hidden_relationship_scene(
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
    start_present: set[str],
    upsert_ids: set[str],
) -> str:
    """Persist departed-NPC numeric updates without conflicting with the visible present-NPC footer."""
    if updates in (None, []):
        return ""
    if not isinstance(updates, list):
        base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "relationship_updates must be an array.")

    final_present = set(compat._current_present_ids(state_after))
    allowed = set(start_present) | set(upsert_ids)
    lines: List[str] = []

    for raw in updates:
        if not isinstance(raw, dict):
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)

        # Present-NPC numeric state comes from the visible footer when available; a redundant
        # relationship_updates row must not bounce the same gameplay turn into another 409.
        if owner_id in final_present:
            continue
        if owner_id not in allowed:
            owner_name = compat._card_display_name(cards, owner_id)
            base._http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                f"{owner_name}: hidden relationship update is allowed only for an NPC who participated in this turn.",
            )

        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            owner_name = compat._card_display_name(cards, owner_id)
            base._http_error(
                409,
                "RELATIONSHIP_UPDATES_INVALID",
                f"{owner_name}: relationship update must include non-empty dimensions.",
            )

        rendered: List[str] = []
        for item in dimensions:
            if not isinstance(item, dict):
                base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship dimension must be an object.")
            label = str(item.get("label") or "").strip()
            value = item.get("value")
            delta = item.get("delta")
            if not label or not relationship_runtime._is_number(value):
                base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship dimension requires label and numeric value.")
            if delta is not None and not relationship_runtime._is_number(delta):
                base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship delta must be numeric.")
            suffix = f"/{delta:+g}" if delta is not None else ""
            rendered.append(f"{label} {value:g}{suffix}")

        lines.append(f"{owner_id} - {'; '.join(rendered)}")

    return "Отношения:\n" + "\n".join(lines) if lines else ""


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
        policy["footer_is_transaction_gate"] = False
        policy["footer_validation"] = (
            "Writer should show present NPC->POV rows, but the server reconciles display mistakes instead of rejecting the gameplay turn. "
            "Missing or partial rows preserve saved dimensions; rows for absent NPCs are ignored for visible persistence; redundant present-NPC relationship_updates do not conflict with the footer."
        )
        policy["instruction"] = (
            "Relationships are persistent but dynamically extensible. Preserve old non-zero dimensions and show the correct rows when possible. "
            "Do not retry a whole scene merely to repair cosmetic footer drift: persistent canon keeps saved values when a row/label is missing, and the server ignores redundant relationship channels."
        )
        context["relationship_policy"] = policy
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
    _install_packet_policy_wrapper()
