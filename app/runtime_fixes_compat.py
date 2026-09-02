from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import runtime_fixes as base, storage
from .session_recovery import current_recovery_status


_ORIGINAL_REWRITE_TURN_PACKET = base._rewrite_turn_packet
_ORIGINAL_RELATIONSHIP_PATCH = base.relationship_patch_from_scene
_ORIGINAL_PREPARE_EXTRACTED = base._prepare_extracted_for_commit
_ORIGINAL_PREPARE_TURN = base.prepare_turn_packet
_ORIGINAL_CONTINUE_SESSION = base.continue_session


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
        metrics: Dict[str, Any] = {}
        if isinstance(relation, dict):
            metrics = {
                str(key): value
                for key, value in relation.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        result[owner_id] = {"metrics": metrics, "has_saved_baseline": bool(metrics)}
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
    policy["source_of_truth"] = "relationship_lens + relationship_contract"
    policy["footer_required_for_every_present_npc"] = True
    policy["fresh_baseline_required"] = True
    policy["authoritative_start_snapshot"] = _relationship_snapshot(state)
    policy["authoritative_start_snapshot_note"] = (
        "Diagnostic start values only. relationship_lens + relationship_contract remain the single relationship model. "
        "An empty metrics object means this present NPC needs a first natural 1-3 dimension baseline in this scene, not an empty footer row."
    )
    policy["footer_validation"] = (
        "Server-enforced: EVERY NPC physically present at scene end must have one NPC->POV row. "
        "Fresh NPCs without saved metrics must establish 1-3 natural dimensions now. Existing saved dimensions must all remain visible; "
        "displayed delta arithmetic for saved dimensions must match the saved start value."
    )
    policy["instruction"] = (
        "Use relationship_lens and relationship_contract as the only relationship model. Every present NPC must appear in the visible Relationships footer. "
        "If the NPC already has saved dimensions, preserve all established labels and values across scenes and absences, changing only what this scene genuinely changes. "
        "If the NPC has no saved dimensions yet, initialize a first natural baseline of 1-3 specific dimensions from that NPC's character, goals, knowledge and current interaction; do not leave Relationships empty. "
        "New dimensions may later be added only when they genuinely arise and must not replace old dimensions. Deltas are optional; when shown for an established dimension, final value must equal saved start value plus delta. "
        "Only NPCs present at scene end are printed. If an NPC's relationship changes during the scene but that NPC leaves before the footer, persist final values through extracted.relationship_updates."
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
    packet["runtime_fix_version"] = 5
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(result)
    result["chunk_count"] = len(chunks)
    result["instruction"] = (
        str(result.get("instruction", "")).rstrip()
        + " Every present NPC requires a visible NPC->POV relationship row; fresh NPCs initialize a 1-3 dimension baseline."
    ).strip()
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
        incoming = footer.get(owner_id)
        if not incoming:
            base._http_error(
                409,
                "RELATIONSHIP_FOOTER_REQUIRED",
                "Every NPC physically present at scene end must appear in the Relationships footer. If no saved baseline exists yet, initialize 1-3 natural NPC->POV dimensions now.",
            )
        baseline = base._numeric_relationships(state_before, owner_id)
        if baseline:
            _validate_dimensions(incoming, baseline)
        elif not 1 <= len(incoming) <= 3:
            base._http_error(
                409,
                "RELATIONSHIP_BASELINE_INVALID",
                "A fresh present NPC must establish 1-3 natural relationship dimensions in the footer.",
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
        elif not 1 <= len(parsed) <= 3:
            base._http_error(
                409,
                "RELATIONSHIP_BASELINE_INVALID",
                "A fresh relationship update must establish 1-3 natural dimensions.",
            )
        lines.append(f"{owner_id} - {'; '.join(rendered)}")

    return "Отношения:\n" + "\n".join(lines) if lines else ""


def _relationship_patch_from_scene(*args, **kwargs):
    callback = kwargs.get("present_character_ids")
    if callback is storage._present_character_ids:
        kwargs["present_character_ids"] = _current_present_ids
    return _ORIGINAL_RELATIONSHIP_PATCH(*args, **kwargs)


def _prepare_extracted_for_commit(*args, **kwargs):
    if not args:
        return _ORIGINAL_PREPARE_EXTRACTED(*args, **kwargs)
    payload = deepcopy(args[0])
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else None
    if isinstance(extracted, dict):
        state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else None
        if isinstance(state_patch, dict) and "current" in state_patch:
            raw_current = state_patch.get("current")
            if not isinstance(raw_current, dict):
                base._http_error(
                    409,
                    "CURRENT_STATE_PATCH_INVALID",
                    "state_patch.current must be an object. Never clear the persistent current scene pointer with null, a list, a string or another replacement value.",
                )
            clean_current: Dict[str, Any] = {}
            for key, value in raw_current.items():
                if key == "present_characters":
                    if value in (None, "", [], {}):
                        base._http_error(
                            409,
                            "CURRENT_STATE_PATCH_INVALID",
                            "current.present_characters cannot be cleared. The POV must remain in the persistent current scene pointer.",
                        )
                    clean_current[key] = deepcopy(value)
                    continue
                if value in (None, "", [], {}):
                    continue
                clean_current[key] = deepcopy(value)
            state_patch["current"] = clean_current
            extracted["state_patch"] = state_patch
            payload["extracted"] = extracted
    return _ORIGINAL_PREPARE_EXTRACTED(payload, *args[1:], **kwargs)


def _prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    status = current_recovery_status(session_id)
    if status["required"]:
        base._http_error(
            409,
            "CURRENT_RECOVERY_REQUIRED",
            "Persistent state.current is empty or unusable. Call recoverSessionCurrent for this same session_id before preparing another gameplay turn. Do not use commitTurn to repair it.",
        )
    return _ORIGINAL_PREPARE_TURN(session_id, user_input)


def _continue_session(session_id: str) -> Dict[str, Any]:
    result = dict(_ORIGINAL_CONTINUE_SESSION(session_id))
    status = current_recovery_status(session_id)
    result["current_recovery_required"] = status["required"]
    result["current_recovery_reasons"] = status["reasons"]
    if status["required"]:
        result["instruction"] = (
            "This exact session still exists, but its technical state.current scene pointer is empty or unusable. "
            "Call recoverSessionCurrent(session_id) before prepareTurn. Recovery must not create a gameplay turn or rewrite chronology/memory/canon."
        )
    return result


def install() -> None:
    base._rewrite_turn_packet = _rewrite_turn_packet
    base._validate_dimensions = _validate_dimensions
    base._validate_visible_footer = _validate_visible_footer
    base._hidden_relationship_scene = _hidden_relationship_scene
    base.relationship_patch_from_scene = _relationship_patch_from_scene
    base._prepare_extracted_for_commit = _prepare_extracted_for_commit
    base.prepare_turn_packet = _prepare_turn_packet
    base.continue_session = _continue_session
    base.install()
