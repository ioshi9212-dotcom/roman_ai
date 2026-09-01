from __future__ import annotations

import json
from typing import Any, Dict, List

from . import runtime_fixes as base, storage


_ORIGINAL_REWRITE_TURN_PACKET = base._rewrite_turn_packet
_ORIGINAL_RELATIONSHIP_PATCH = base.relationship_patch_from_scene


def _current_present_ids(state: Dict[str, Any]) -> List[str]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    elif isinstance(present, str):
        present = [present]
    elif not isinstance(present, list):
        present = []

    result: List[str] = []
    for value in present:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            result.append(str(value))

    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = pov.get("character_id")
    if pov_id:
        result.append(str(pov_id))
    return list(dict.fromkeys(result))


def _relationship_snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    result: Dict[str, Any] = {}
    for owner_id in _current_present_ids(state):
        if owner_id == pov_id:
            continue
        relation = relationships.get(owner_id)
        if not isinstance(relation, dict):
            continue
        metrics = {
            str(key): value
            for key, value in relation.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if metrics:
            result[owner_id] = {"metrics": metrics}
    return result


def _rewrite_turn_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    result = _ORIGINAL_REWRITE_TURN_PACKET(session_id, manifest)
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return result
    context = json.loads(raw)

    policy = context.get("relationship_policy")
    if not isinstance(policy, dict):
        policy = {}
    state = context.get("scene_state") if isinstance(context.get("scene_state"), dict) else {}
    policy["authoritative_start_snapshot"] = _relationship_snapshot(state)
    policy["authoritative_start_snapshot_note"] = (
        "Compatibility diagnostic only. relationship_lens + relationship_contract remain the single relationship model."
    )
    policy["footer_validation"] = (
        "Server-enforced: only NPCs present at scene end may appear; every established dimension for a present NPC must be shown; "
        "displayed delta arithmetic must match the saved start value."
    )
    context["relationship_policy"] = policy

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [
        text[i : i + storage.MAX_PACKET_CHARS]
        for i in range(0, len(text), storage.MAX_PACKET_CHARS)
    ] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    packet["runtime_fix_version"] = 3
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(result)
    result["chunk_count"] = len(chunks)
    return result


def _validate_dimensions(
    incoming: List[Dict[str, Any]],
    baseline: Dict[str, int | float],
) -> None:
    if not incoming:
        base._http_error(
            409,
            "RELATIONSHIP_FOOTER_REQUIRED",
            "A present NPC with a saved relationship must appear in the Relationships footer.",
        )

    baseline_by_norm = {
        base._relationship_norm(label): value for label, value in baseline.items()
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
                f"Relationship dimension {label!r} is duplicated in the footer.",
            )
        incoming_by_norm[normalized] = item

    missing = set(baseline_by_norm) - set(incoming_by_norm)
    if missing:
        base._http_error(
            409,
            "RELATIONSHIP_DIMENSIONS_INCOMPLETE",
            "The footer must show every established relationship dimension for a present NPC.",
        )

    for normalized, old_value in baseline_by_norm.items():
        item = incoming_by_norm.get(normalized)
        if not item:
            continue
        delta = item.get("delta")
        if delta is None:
            continue
        expected = old_value + delta
        if abs(float(item.get("value")) - float(expected)) > 1e-9:
            base._http_error(
                409,
                "RELATIONSHIP_ARITHMETIC_MISMATCH",
                "Relationship final value does not equal saved value plus displayed delta.",
            )


def _validate_visible_footer(
    scene_output: str,
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    footer = base._parse_footer(
        scene_output,
        cards=cards,
        resolve_character_id=base._resolve_character_id,
    )
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    final_present = set(_current_present_ids(state_after))

    for owner_id in footer:
        if owner_id == pov_id:
            base._http_error(
                409,
                "RELATIONSHIP_DIRECTION_INVALID",
                "Relationships footer may contain NPC -> POV only, never POV -> NPC.",
            )
        if owner_id not in final_present:
            base._http_error(
                409,
                "RELATIONSHIP_FOOTER_ABSENT_NPC",
                "An NPC absent at scene end must not be printed in the visible Relationships footer.",
            )

    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        baseline = base._numeric_relationships(state_before, owner_id)
        if not baseline:
            continue
        incoming = footer.get(owner_id)
        if not incoming:
            base._http_error(
                409,
                "RELATIONSHIP_FOOTER_REQUIRED",
                "A present NPC with saved relationship dimensions is missing from the Relationships footer.",
            )
        _validate_dimensions(incoming, baseline)
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
    if updates in (None, []):
        return ""
    if not isinstance(updates, list):
        base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "relationship_updates must be an array.")

    final_present = set(_current_present_ids(state_after))
    allowed = set(start_present) | set(upsert_ids)
    lines: List[str] = []

    for raw in updates:
        if not isinstance(raw, dict):
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)
        if owner_id in final_present:
            base._http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_PRESENT_NPC",
                "NPCs still present at scene end must be persisted through the visible footer.",
            )
        if owner_id not in allowed:
            base._http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                "Hidden relationship update is allowed only for an NPC who participated in this turn.",
            )

        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            base._http_error(
                409,
                "RELATIONSHIP_UPDATES_INVALID",
                "Each relationship update must include non-empty dimensions.",
            )

        parsed: List[Dict[str, Any]] = []
        rendered: List[str] = []
        for item in dimensions:
            if not isinstance(item, dict):
                base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship dimension must be an object.")
            label = str(item.get("label") or "").strip()
            value = item.get("value")
            delta = item.get("delta")
            if not label or not isinstance(value, (int, float)) or isinstance(value, bool):
                base._http_error(
                    409,
                    "RELATIONSHIP_UPDATES_INVALID",
                    "Relationship dimension requires label and numeric value.",
                )
            if delta is not None and (
                not isinstance(delta, (int, float)) or isinstance(delta, bool)
            ):
                base._http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship delta must be numeric.")
            parsed.append({"label": label, "value": value, "delta": delta})
            suffix = f"/{delta:+g}" if delta is not None else ""
            rendered.append(f"{label} {value:g}{suffix}")

        baseline = base._numeric_relationships(state_before, owner_id)
        if baseline:
            _validate_dimensions(parsed, baseline)
        lines.append(f"{owner_id} - {'; '.join(rendered)}")

    return "Отношения:\n" + "\n".join(lines) if lines else ""


def _relationship_patch_from_scene(*args, **kwargs):
    callback = kwargs.get("present_character_ids")
    if callback is storage._present_character_ids:
        kwargs["present_character_ids"] = _current_present_ids
    return _ORIGINAL_RELATIONSHIP_PATCH(*args, **kwargs)


def install() -> None:
    base._rewrite_turn_packet = _rewrite_turn_packet
    base._validate_dimensions = _validate_dimensions
    base._validate_visible_footer = _validate_visible_footer
    base._hidden_relationship_scene = _hidden_relationship_scene
    base.relationship_patch_from_scene = _relationship_patch_from_scene
    base.install()
