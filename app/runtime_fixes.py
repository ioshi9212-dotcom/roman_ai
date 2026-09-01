from __future__ import annotations

import json
import threading
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import audit_runtime, session_runtime as legacy_runtime, storage
from .relationship_runtime import (
    _norm as _relationship_norm,
    _parse_footer,
    overwrite_relationship_snapshots,
    relationship_patch_from_scene,
    repair_relationship_state,
)
from .session_runtime import (
    _canonicalize_state_character_refs,
    _clear_legacy_handoff,
    _normalise_chronology_events,
    _normalise_memory_event_ids,
    _refresh_session_familiarity,
    _resolve_character_id,
)


_FIX_VERSION = 1
_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: Dict[str, threading.RLock] = {}

_ORIGINAL_PREPARE_TURN = legacy_runtime.prepare_turn_packet
_ORIGINAL_CONTINUE_SESSION = legacy_runtime.continue_session
_ORIGINAL_GET_TURN_PACKET_CHUNK = storage.get_turn_packet_chunk
_ORIGINAL_SAVE_NOVEL = storage.save_novel
_ORIGINAL_GET_NOVEL = storage.get_novel
_ORIGINAL_GET_AUDIT_SNAPSHOT = audit_runtime.get_audit_snapshot
_ORIGINAL_GET_AUDIT_CHUNK = audit_runtime.get_audit_snapshot_chunk
_ORIGINAL_REQUIRE_AUDIT_READ = audit_runtime.require_complete_audit_read
_ORIGINAL_CLEAR_AUDIT_PACKET = audit_runtime.clear_audit_packet


def _session_lock(session_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(str(session_id), threading.RLock())


def _http_error(status_code: int, code: str, detail: str) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": detail},
    )


def validate_novel_id(novel_id: Any) -> str:
    value = str(novel_id or "").strip()
    if not value or len(value) > 120:
        _http_error(422, "INVALID_NOVEL_ID", "novel_id must contain 1-120 characters")
    if value in {".", ".."} or "/" in value or "\\" in value:
        _http_error(422, "INVALID_NOVEL_ID", "novel_id cannot contain path separators")
    if any(ord(ch) < 32 for ch in value):
        _http_error(422, "INVALID_NOVEL_ID", "novel_id contains control characters")
    return value


def save_novel(template: Dict[str, Any]) -> Dict[str, Any]:
    validate_novel_id(template.get("novel_id"))
    return _ORIGINAL_SAVE_NOVEL(template)


def get_novel(novel_id: str) -> Dict[str, Any]:
    return _ORIGINAL_GET_NOVEL(validate_novel_id(novel_id))


def _rewrite_turn_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    if packet.get("runtime_fix_version") == _FIX_VERSION:
        return manifest

    raw = "".join(packet.get("chunks", []))
    if not raw:
        return manifest
    context = json.loads(raw)

    context["relationship_policy"] = {
        "direction": "NPC -> POV only",
        "source_of_truth": "relationship_lens + relationship_contract",
        "instruction": (
            "Use relationship_lens and relationship_contract as the only relationship model. "
            "Preserve established dimensions across scenes and absences. New dimensions may be added only when they genuinely arise "
            "and must not replace old dimensions. Deltas are optional; when a delta is shown for an established dimension, "
            "the final value must equal the saved start value plus that delta. The visible Relationships footer contains only NPCs "
            "present at scene end. If an NPC's relationship changes during the scene but that NPC leaves before the footer, persist "
            "the final values invisibly through extracted.relationship_updates instead of printing an absent NPC in the footer."
        ),
    }

    persistence = context.get("persistence_contract")
    if not isinstance(persistence, dict):
        persistence = {}
    persistence["relationship_updates"] = {
        "optional": True,
        "when": "Only for an NPC whose relationship changed in this turn and who is absent at scene end.",
        "format": (
            '[{"character_id":"npc_id","dimensions":'
            '[{"label":"доверие","value":12,"delta":2}]}]'
        ),
        "instruction": (
            "Do not use relationship_updates for NPCs still present at scene end; their visible footer is authoritative. "
            "For a departed NPC include all already-established dimensions, plus any genuinely new dimensions."
        ),
    }
    context["persistence_contract"] = persistence

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [
        text[i : i + storage.MAX_PACKET_CHARS]
        for i in range(0, len(text), storage.MAX_PACKET_CHARS)
    ] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    packet["runtime_fix_version"] = _FIX_VERSION
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(manifest)
    result["chunk_count"] = len(chunks)
    result["instruction"] = (
        str(result.get("instruction", "")).rstrip()
        + " Relationship persistence follows relationship_lens + relationship_contract; "
        "departed-NPC changes use extracted.relationship_updates."
    ).strip()
    return result


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    with _session_lock(session_id):
        manifest = _ORIGINAL_PREPARE_TURN(session_id, user_input)
        return _rewrite_turn_packet(session_id, manifest)


def get_turn_packet_chunk(session_id: str, packet_id: str, chunk_index: int) -> Dict[str, Any]:
    with _session_lock(session_id):
        return _ORIGINAL_GET_TURN_PACKET_CHUNK(session_id, packet_id, chunk_index)


def _numeric_relationships(state: Dict[str, Any], owner_id: str) -> Dict[str, int | float]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    relation = relationships.get(owner_id) if isinstance(relationships, dict) else None
    if not isinstance(relation, dict):
        return {}
    return {
        str(label): value
        for label, value in relation.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _validate_dimensions(
    incoming: List[Dict[str, Any]],
    baseline: Dict[str, int | float],
) -> None:
    if not incoming:
        _http_error(
            409,
            "RELATIONSHIP_FOOTER_REQUIRED",
            "A present NPC with a saved relationship must appear in the Relationships footer.",
        )

    baseline_by_norm = {_relationship_norm(label): value for label, value in baseline.items()}
    incoming_by_norm: Dict[str, Dict[str, Any]] = {}
    for item in incoming:
        label = str(item.get("label") or "").strip()
        normalized = _relationship_norm(label)
        if not normalized:
            continue
        if normalized in incoming_by_norm:
            _http_error(
                409,
                "RELATIONSHIP_DIMENSION_DUPLICATE",
                f"Relationship dimension {label!r} is duplicated in the footer.",
            )
        incoming_by_norm[normalized] = item

    missing = set(baseline_by_norm) - set(incoming_by_norm)
    if missing:
        _http_error(
            409,
            "RELATIONSHIP_DIMENSIONS_INCOMPLETE",
            "The footer must preserve every established relationship dimension for a present NPC.",
        )

    for normalized, old_value in baseline_by_norm.items():
        item = incoming_by_norm.get(normalized)
        if not item:
            continue
        delta = item.get("delta")
        if delta is None:
            continue
        final_value = item.get("value")
        expected = old_value + delta
        if abs(float(final_value) - float(expected)) > 1e-9:
            _http_error(
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
    footer = _parse_footer(
        scene_output,
        cards=cards,
        resolve_character_id=_resolve_character_id,
    )
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    final_present = set(str(value) for value in storage._present_character_ids(state_after))

    for owner_id in footer:
        if owner_id == pov_id:
            _http_error(
                409,
                "RELATIONSHIP_DIRECTION_INVALID",
                "Relationships footer may contain NPC -> POV only, never POV -> NPC.",
            )
        if owner_id not in final_present:
            _http_error(
                409,
                "RELATIONSHIP_FOOTER_ABSENT_NPC",
                "An NPC absent at scene end must not be printed in the visible Relationships footer.",
            )

    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        baseline = _numeric_relationships(state_before, owner_id)
        if not baseline:
            continue
        incoming = footer.get(owner_id)
        if not incoming:
            _http_error(
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
        _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "relationship_updates must be an array.")

    final_present = set(str(value) for value in storage._present_character_ids(state_after))
    allowed = set(start_present) | set(upsert_ids)
    lines: List[str] = []

    for raw in updates:
        if not isinstance(raw, dict):
            _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)
        if owner_id in final_present:
            _http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_PRESENT_NPC",
                "NPCs still present at scene end must be persisted through the visible footer.",
            )
        if owner_id not in allowed:
            _http_error(
                409,
                "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                "Hidden relationship update is allowed only for an NPC who participated in this turn.",
            )

        dimensions = raw.get("dimensions")
        if not isinstance(dimensions, list) or not dimensions:
            _http_error(
                409,
                "RELATIONSHIP_UPDATES_INVALID",
                "Each relationship update must include non-empty dimensions.",
            )

        parsed: List[Dict[str, Any]] = []
        rendered: List[str] = []
        for item in dimensions:
            if not isinstance(item, dict):
                _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship dimension must be an object.")
            label = str(item.get("label") or "").strip()
            value = item.get("value")
            delta = item.get("delta")
            if not label or not isinstance(value, (int, float)) or isinstance(value, bool):
                _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship dimension requires label and numeric value.")
            if delta is not None and (
                not isinstance(delta, (int, float)) or isinstance(delta, bool)
            ):
                _http_error(409, "RELATIONSHIP_UPDATES_INVALID", "Relationship delta must be numeric.")
            parsed.append({"label": label, "value": value, "delta": delta})
            suffix = f"/{delta:+g}" if delta is not None else ""
            rendered.append(f"{label} {value:g}{suffix}")

        baseline = _numeric_relationships(state_before, owner_id)
        if baseline:
            _validate_dimensions(parsed, baseline)
        lines.append(f"{owner_id} - {'; '.join(rendered)}")

    return "Отношения:\n" + "\n".join(lines) if lines else ""


def _mark_relationship_change_turn(
    patch: Dict[str, Any],
    *,
    state_before: Dict[str, Any],
    turn_number: int,
) -> Dict[str, Any]:
    docs = patch.get("relationship_documents")
    relationships = patch.get("relationships")
    if not isinstance(docs, dict) or not isinstance(relationships, dict):
        return patch

    before = state_before.get("relationships")
    before = before if isinstance(before, dict) else {}
    pov = state_before.get("pov") if isinstance(state_before.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    result = deepcopy(patch)
    docs = result.get("relationship_documents", {})
    for owner_id, after_relation in relationships.items():
        if not isinstance(after_relation, dict) or after_relation == before.get(owner_id):
            continue
        doc = docs.get(owner_id)
        if not isinstance(doc, dict):
            continue
        relations = doc.get("relations") if isinstance(doc.get("relations"), list) else []
        for relation in relations:
            if (
                isinstance(relation, dict)
                and str(relation.get("target_character_id") or "") == pov_id
            ):
                relation["last_changed_turn"] = turn_number
    return result


def _prepare_extracted_for_commit(
    payload: Dict[str, Any],
    *,
    root,
    turn_number: int,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    extracted = payload.get("extracted")
    if not isinstance(extracted, dict) or extracted.get("persistence_reviewed") is not True:
        raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")
    required_lists = ("chronology", "knowledge_add", "experiences_add", "dialogue_memory_add")
    if any(not isinstance(extracted.get(field), list) for field in required_lists):
        raise RuntimeError("PERSISTENCE_REVIEW_REQUIRED")

    source = storage._read_json(root / "source.json", {})
    old_cards = storage._load_cards(root, source)
    cards = storage._apply_character_upserts(old_cards, extracted)
    upsert_ids = {
        storage._card_id(item)
        for item in extracted.get("character_upserts", [])
        if isinstance(item, dict) and storage._card_id(item)
    }
    turns = storage._read_turns(root)

    state_before = storage._read_json(root / "state.json", {})
    state_before = _canonicalize_state_character_refs(cards, state_before)
    state_before = repair_relationship_state(
        state_before,
        source=source,
        turns=turns,
        cards=cards,
        resolve_character_id=_resolve_character_id,
    )
    start_present = set(str(value) for value in storage._present_character_ids(state_before))

    result = _normalise_memory_event_ids(extracted, turn_number)
    state_patch = deepcopy(result.get("state_patch")) if isinstance(result.get("state_patch"), dict) else {}
    for forbidden in ("relationships", "relationship_schemas", "relationship_documents"):
        state_patch.pop(forbidden, None)

    state_after = storage._deep_merge(state_before, state_patch) if state_patch else deepcopy(state_before)
    state_after = _canonicalize_state_character_refs(cards, state_after)

    current = state_after.get("current") if isinstance(state_after.get("current"), dict) else {}
    if isinstance(state_patch.get("current"), dict) and "present_characters" in state_patch["current"]:
        state_patch.setdefault("current", {})["present_characters"] = deepcopy(
            current.get("present_characters", [])
        )

    _validate_visible_footer(
        payload.get("scene_output", ""),
        cards=cards,
        state_before=state_before,
        state_after=state_after,
    )

    visible_patch = relationship_patch_from_scene(
        payload.get("scene_output", ""),
        cards=cards,
        state=state_after,
        resolve_character_id=_resolve_character_id,
        present_character_ids=storage._present_character_ids,
    )
    working_state = storage._deep_merge(state_after, visible_patch) if visible_patch else state_after

    hidden_scene = _hidden_relationship_scene(
        result.get("relationship_updates"),
        cards=cards,
        state_before=state_before,
        state_after=state_after,
        start_present=start_present,
        upsert_ids=upsert_ids,
    )
    if hidden_scene:
        scene_union = (
            set(start_present)
            | set(str(value) for value in storage._present_character_ids(state_after))
            | upsert_ids
        )
        hidden_patch = relationship_patch_from_scene(
            hidden_scene,
            cards=cards,
            state=working_state,
            resolve_character_id=_resolve_character_id,
            present_character_ids=lambda _state: list(scene_union),
        )
        working_state = storage._deep_merge(working_state, hidden_patch) if hidden_patch else working_state

    relationship_patch: Dict[str, Any] = {}
    for key in ("relationships", "relationship_documents"):
        if working_state.get(key) != state_after.get(key):
            relationship_patch[key] = deepcopy(working_state.get(key, {}))
    relationship_patch = _mark_relationship_change_turn(
        relationship_patch,
        state_before=state_before,
        turn_number=turn_number,
    )

    result["state_patch"] = storage._deep_merge(state_patch, relationship_patch)
    result["chronology"] = _normalise_chronology_events(
        result.get("chronology"),
        turn_number=turn_number,
        state=working_state,
        cards=cards,
    )
    return result, relationship_patch


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _session_lock(session_id):
        root = storage.SESSIONS_DIR / session_id
        if not root.exists():
            raise FileNotFoundError(session_id)
        meta = storage._read_json(root / "meta.json", {})
        _clear_legacy_handoff(root, meta)
        turn_number = int(meta.get("turn_number", 0)) + 1

        payload = deepcopy(payload)
        prepared, relationship_patch = _prepare_extracted_for_commit(
            payload,
            root=root,
            turn_number=turn_number,
        )
        payload["extracted"] = prepared

        result = storage.commit_turn(session_id, payload)
        if relationship_patch:
            persisted_state = storage._read_json(root / "state.json", {})
            persisted_state = overwrite_relationship_snapshots(
                persisted_state, relationship_patch
            )
            storage._write_json(root / "state.json", persisted_state)

        meta = storage._read_json(root / "meta.json", {})
        _clear_legacy_handoff(root, meta)
        _refresh_session_familiarity(session_id)

        result = dict(result)
        result["handoff_required"] = False
        result["saved_chronology_events"] = len(prepared.get("chronology", []))
        result["relationships_persisted_from_footer"] = bool(
            relationship_patch.get("relationships")
        )
        result["relationship_runtime_fix"] = _FIX_VERSION
        return result


def _repair_turn(
    raw: Dict[str, Any],
    *,
    start_turn: int,
    end_turn: int,
    keys: Iterable[str],
) -> int:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        try:
            turn = int(value)
        except (TypeError, ValueError):
            continue
        if start_turn <= turn <= end_turn:
            return turn
        _http_error(
            409,
            "AUDIT_REPAIR_TURN_OUT_OF_RANGE",
            "Audit repair turn must be inside the exact audited range.",
        )
    _http_error(
        409,
        "AUDIT_REPAIR_TURN_REQUIRED",
        "Chronology and memory repairs must include the original turn where the fact/event happened.",
    )
    raise AssertionError("unreachable")


def _prepare_audit_repairs(
    repairs: Dict[str, Any],
    *,
    start_turn: int,
    end_turn: int,
    cards: List[Dict[str, Any]],
) -> Dict[str, Any]:
    result = deepcopy(repairs)

    chronology = result.get("chronology_add")
    if chronology is not None:
        if not isinstance(chronology, list):
            _http_error(409, "AUDIT_REPAIRS_INVALID", "chronology_add must be an array.")
        normalized: List[Dict[str, Any]] = []
        empty_state = {"current": {}, "pov": {}}
        for index, raw in enumerate(chronology):
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            turn = _repair_turn(
                item,
                start_turn=start_turn,
                end_turn=end_turn,
                keys=("turn_number", "turn", "source_turn"),
            )
            item.setdefault("event_id", f"audit_chrono_t{turn}_{index + 1}")
            normalized.extend(
                _normalise_chronology_events(
                    [item],
                    turn_number=turn,
                    state=empty_state,
                    cards=cards,
                )
            )
        result["chronology_add"] = normalized

    for field, target_key, id_key, prefix, candidates in (
        (
            "knowledge_add",
            "learned_turn",
            "fact_id",
            "audit_fact",
            ("learned_turn", "turn_number", "source_turn", "turn"),
        ),
        (
            "experiences_add",
            "turn",
            "event_id",
            "audit_exp",
            ("turn", "turn_number", "source_turn"),
        ),
        (
            "dialogue_memory_add",
            "turn",
            "topic_id",
            "audit_dialogue",
            ("turn", "turn_number", "source_turn"),
        ),
    ):
        values = result.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            _http_error(409, "AUDIT_REPAIRS_INVALID", f"{field} must be an array.")
        fixed = []
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            turn = _repair_turn(
                item,
                start_turn=start_turn,
                end_turn=end_turn,
                keys=candidates,
            )
            item[target_key] = turn
            item.setdefault(id_key, f"{prefix}_t{turn}_{index + 1}")
            fixed.append(item)
        result[field] = fixed

    return result


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _session_lock(session_id):
        root = storage.SESSIONS_DIR / session_id
        if not root.exists():
            raise FileNotFoundError(session_id)

        meta = storage._read_json(root / "meta.json", {})
        _clear_legacy_handoff(root, meta)
        if not meta.get("audit_required"):
            raise RuntimeError("AUDIT_NOT_REQUIRED")

        start_turn = int(payload.get("start_turn", 0))
        end_turn = int(payload.get("end_turn", 0))
        _ORIGINAL_REQUIRE_AUDIT_READ(session_id, start_turn, end_turn)

        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        prepared = deepcopy(payload)
        repairs = prepared.get("repairs") if isinstance(prepared.get("repairs"), dict) else {}
        prepared["repairs"] = _prepare_audit_repairs(
            repairs,
            start_turn=start_turn,
            end_turn=end_turn,
            cards=cards,
        )

        result = storage.commit_audit(session_id, prepared)
        meta = storage._read_json(root / "meta.json", {})
        _clear_legacy_handoff(root, meta)
        _refresh_session_familiarity(session_id)
        result = dict(result)
        result["handoff_required"] = False
        result["audit_runtime_fix"] = _FIX_VERSION
        return result


def _rewrite_audit_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    path = root / audit_runtime.AUDIT_PACKET_FILE
    packet = storage._read_json(path, {})
    if packet.get("runtime_fix_version") == _FIX_VERSION:
        return manifest
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return manifest
    payload = json.loads(raw)
    payload["audit_repair_policy"] = {
        "mandatory_original_turn": True,
        "chronology_add": "Each repair must include turn_number/turn/source_turn from the exact audited turn where the event happened.",
        "knowledge_add": "Each repair must include learned_turn or turn_number/source_turn.",
        "experiences_add": "Each repair must include turn or turn_number/source_turn.",
        "dialogue_memory_add": "Each repair must include turn or turn_number/source_turn.",
        "instruction": (
            "Never stamp a repair with the audit-end turn merely because the repair is discovered during the audit. "
            "Preserve the original causal turn. Include story_date/period/location/participants from exact evidence when known; "
            "leave unknown optional context absent rather than borrowing the current final scene."
        ),
    }
    payload["instruction"] = (
        str(payload.get("instruction", "")).rstrip()
        + " Every chronology or memory repair MUST carry its original turn inside the audited range; "
        "repairs without an original turn are rejected."
    ).strip()

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    chunks = [
        text[i : i + storage.MAX_PACKET_CHARS]
        for i in range(0, len(text), storage.MAX_PACKET_CHARS)
    ] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    packet["runtime_fix_version"] = _FIX_VERSION
    storage._write_json(path, packet)

    result = dict(manifest)
    result["chunk_count"] = len(chunks)
    result["total_chars"] = len(text)
    result["already_read_chunks"] = []
    return result


def get_audit_snapshot(session_id: str) -> Dict[str, Any]:
    with _session_lock(session_id):
        manifest = _ORIGINAL_GET_AUDIT_SNAPSHOT(session_id)
        return _rewrite_audit_packet(session_id, manifest)


def get_audit_snapshot_chunk(
    session_id: str, audit_id: str, chunk_index: int
) -> Dict[str, Any]:
    with _session_lock(session_id):
        return _ORIGINAL_GET_AUDIT_CHUNK(session_id, audit_id, chunk_index)


def require_complete_audit_read(session_id: str, start_turn: int, end_turn: int) -> None:
    with _session_lock(session_id):
        _ORIGINAL_REQUIRE_AUDIT_READ(session_id, start_turn, end_turn)


def clear_audit_packet(session_id: str) -> None:
    with _session_lock(session_id):
        _ORIGINAL_CLEAR_AUDIT_PACKET(session_id)


def continue_session(session_id: str) -> Dict[str, Any]:
    with _session_lock(session_id):
        return _ORIGINAL_CONTINUE_SESSION(session_id)


def install() -> None:
    # main.py imports these module attributes after app.__init__ runs, so the public
    # API keeps the same operationIds while using the corrected runtime behavior.
    legacy_runtime.prepare_turn_packet = prepare_turn_packet
    legacy_runtime.commit_turn = commit_turn
    legacy_runtime.commit_audit = commit_audit
    legacy_runtime.continue_session = continue_session

    audit_runtime.get_audit_snapshot = get_audit_snapshot
    audit_runtime.get_audit_snapshot_chunk = get_audit_snapshot_chunk
    audit_runtime.require_complete_audit_read = require_complete_audit_read
    audit_runtime.clear_audit_packet = clear_audit_packet

    storage.get_turn_packet_chunk = get_turn_packet_chunk
    storage.save_novel = save_novel
    storage.get_novel = get_novel
