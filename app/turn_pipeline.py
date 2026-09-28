from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from fastapi import HTTPException

from . import (
    audit_runtime,
    cast_registry_runtime,
    character_chunk_read,
    chronology_integrity_runtime,
    fast_audit_runtime,
    game_day,
    knowledge_firewall_runtime,
    living_world_runtime,
    memory_integrity_runtime,
    narrative_guardrails_runtime,
    npc_intent_runtime,
    relationship_growth_runtime,
    relationship_policy_runtime,
    relationship_runtime,
    resume_compact_runtime,
    runtime_fixes,
    runtime_fixes_compat,
    scene_logic_runtime,
    scene_presence_runtime,
    session_recovery,
    session_runtime,
    simple_profile_runtime,
    stability_runtime,
    storage,
    story_thread_runtime,
    transport_scope_runtime,
    writer_first_runtime,
)
from .transactional_storage import session_transaction


# Capture the real core once, before any gameplay patching.
_BASE_PREPARE = session_runtime.prepare_turn_packet
_BASE_AUDIT = session_runtime.commit_audit
_BASE_CONTINUE = session_runtime.continue_session
_BASE_PARTICIPATION_BUNDLE = character_chunk_read._participation_bundle
_BASE_CREATE_SESSION = storage.create_session
_BASE_RECOVER_CURRENT = session_recovery.recover_session_current

PIPELINE_VERSION = 2


def _packet_manifest(packet: Dict[str, Any], *, reused: bool) -> Dict[str, Any]:
    chunks = packet.get("chunks", []) if isinstance(packet.get("chunks"), list) else []
    read = list(packet.get("read_chunks", [])) if isinstance(packet.get("read_chunks"), list) else []
    unread = [index for index in range(len(chunks)) if index not in set(read)]
    result = {
        "packet_id": packet.get("packet_id"),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "chunk_count": len(chunks),
        "total_chars": sum(len(str(chunk)) for chunk in chunks),
        "relevant_character_ids": [str(value) for value in packet.get("relevant_character_ids", []) if value],
        "working_context": True,
        "writer_first": True,
        "writer_first_version": writer_first_runtime.WRITER_FIRST_VERSION,
        "first_chunk_included": bool(chunks),
        "reused_pending_packet": reused,
        "read_chunks": read,
        "next_chunk_index": unread[0] if unread else None,
        "all_chunks_read": not unread,
        "turn_pipeline_version": PIPELINE_VERSION,
        "instruction": (
            "Pending packet reused. Read only unread chunks and commit once."
            if reused
            else "Chunk 0 is included. Read remaining unread chunks, then write and commit once."
        ),
    }
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
    return result


def _read_packet_context(root) -> tuple[Dict[str, Any], Dict[str, Any]]:
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
    if not raw:
        return packet, {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {}
    return packet, value if isinstance(value, dict) else {}


def _write_packet_context(root, packet: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    size = writer_first_runtime.WRITER_PACKET_CHARS
    chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
    packet = deepcopy(packet)
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = [0]
    packet["writer_first_version"] = writer_first_runtime.WRITER_FIRST_VERSION
    packet["writer_first_payload_chars"] = len(text)
    packet["turn_pipeline_version"] = PIPELINE_VERSION
    storage._write_json(root / "turn_packet.json", packet)
    return packet


def _inject_scene_presence(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet, context = _read_packet_context(root)
    if not context:
        return base

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = context.get("scene_state") if isinstance(context.get("scene_state"), dict) else storage._read_json(root / "state.json", {})
    start_roster = scene_presence_runtime._present_ids(cards, state)
    remote_roster = scene_presence_runtime._remote_ids(cards, state)
    scene_roster = list(dict.fromkeys([*start_roster, *remote_roster]))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = scene_presence_runtime._resolve_character_id(cards, pov.get("character_id")) or str(pov.get("character_id") or "")

    context["scene_focus"] = {
        "pov_character_id": pov_id or None,
        "present_character_ids": start_roster,
        "remote_character_ids": remote_roster,
        "required_full_character_ids": scene_roster,
        "instruction": (
            "Present characters remain present until an explicit leave. Remote contact is a participant "
            "for profile/knowledge/relationship context but has no physical position."
        ),
    }
    context["scene_presence"] = {
        "start_present_character_ids": start_roster,
        "start_remote_character_ids": remote_roster,
        "roster": [
            {
                "character_id": cid,
                "name": scene_presence_runtime._card_name(cards, cid),
                "full_card_path": f"character_cards[character_id={cid}]",
                "memory_path": f"character_memory[{cid}]",
            }
            for cid in scene_roster
        ],
        "final_roster_formula": "start roster + enter - leave; move does not change membership",
        "pov_must_remain_present": True,
        "direct_roster_omission_cannot_remove": True,
        "presence_updates": {
            "field": "extracted.presence_updates",
            "actions": ["enter", "leave", "move"],
        },
    }

    persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
    persistence["presence_updates"] = {
        "optional": True,
        "required_when": "Only on enter/leave/move.",
        "rule": "No presence update means the start roster persists.",
    }
    context["persistence_contract"] = persistence

    packet = _write_packet_context(root, packet, context)
    result = dict(base)
    result.update(_packet_manifest(packet, reused=False))
    return result


def _apply_relationship_growth_packet_policy(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet, context = _read_packet_context(root)
    if not context:
        return base

    lens = context.get("relationship_lens") if isinstance(context.get("relationship_lens"), dict) else {}
    lens = deepcopy(lens)
    lens["initialization_required"] = False
    lens["initialization_instruction"] = "Новые dimensions только по реальному основанию; старые сохраняются."
    context["relationship_lens"] = lens
    context["relationship_lens_instruction"] = "relationship_lens — текущий канон NPC->POV."

    policy = context.get("relationship_policy") if isinstance(context.get("relationship_policy"), dict) else {}
    policy = deepcopy(policy)
    policy.update({
        "source_of_truth": "persistent relationship state + causal relationship_updates",
        "footer_is_display_only": True,
        "footer_is_transaction_gate": False,
        "footer_required_for_every_present_npc": False,
        "fresh_baseline_required": False,
        "zero_dimensions_may_be_hidden": True,
        "new_dimensions_may_be_appended": True,
    })
    context["relationship_policy"] = policy

    packet = _write_packet_context(root, packet, context)
    result = dict(base)
    result.update(_packet_manifest(packet, reused=False))
    return result


def _writer_first_rewrite(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet, context = _read_packet_context(root)
    if not context:
        return base

    persistent_state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    context = stability_runtime._compact_turn_context(context, source)
    context = transport_scope_runtime._strip_legacy_full_payloads(
        context,
        persistent_state=persistent_state,
    )
    context = writer_first_runtime._rewrite_context(session_id, context)
    packet = _write_packet_context(root, packet, context)
    result = writer_first_runtime._manifest(packet, base)
    result["working_context"] = True
    result["reused_pending_packet"] = False
    return result


def _current_pointer_guard(session_id: str) -> None:
    status = session_recovery.current_recovery_status(session_id)
    if not status.get("required"):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "CURRENT_RECOVERY_REQUIRED",
            "reasons": status.get("reasons", []),
            "instruction": (
                "Repair the technical current scene pointer with recoverSessionCurrent before preparing another gameplay turn."
            ),
        },
    )


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    stability_runtime._recover_session(session_id)
    _current_pointer_guard(session_id)
    game_day._sync_session_game_day(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        expected_turn = int(meta.get("turn_number", 0) or 0) + 1
        pending = storage._read_json(root / "turn_packet.json", {})
        if (
            isinstance(pending, dict)
            and pending.get("packet_id")
            and int(pending.get("prepared_for_turn", 0) or 0) == expected_turn
            and str(pending.get("user_input") or "") == str(user_input)
            and isinstance(pending.get("chunks"), list)
            and pending.get("chunks")
        ):
            return _packet_manifest(pending, reused=True)

    # One base prepare, then an explicit ordered transform list.
    result = dict(_BASE_PREPARE(session_id, user_input))
    result = runtime_fixes._rewrite_turn_packet(session_id, result)
    result = runtime_fixes_compat._rewrite_turn_packet(session_id, result)
    result = _apply_relationship_growth_packet_policy(session_id, result)
    result = _inject_scene_presence(session_id, result)
    result = _writer_first_rewrite(session_id, result)

    # Same legacy behavior, but no transform wraps another transform.
    result = narrative_guardrails_runtime._rewrite_packet(session_id, result)
    result = story_thread_runtime._rewrite_story_drive(session_id, result)
    result = living_world_runtime._rewrite_packet(session_id, result)
    result = scene_logic_runtime._rewrite_packet(session_id, result)
    result = cast_registry_runtime._rewrite_packet(session_id, result)
    result = relationship_policy_runtime._rewrite_packet(session_id, result)
    result = knowledge_firewall_runtime._rewrite_packet(session_id, result)
    result = simple_profile_runtime._rewrite_packet(session_id, result)

    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        packet["turn_pipeline_version"] = PIPELINE_VERSION
        storage._write_json(root / "turn_packet.json", packet)
        final = _packet_manifest(packet, reused=False)
        # Keep feature flags returned by explicit transforms.
        final.update({
            key: value
            for key, value in result.items()
            if key not in final and key not in {"content"}
        })
        final["content"] = packet.get("chunks", [""])[0] if packet.get("chunks") else ""
        final["turn_pipeline_version"] = PIPELINE_VERSION
        _, final_context = _read_packet_context(root)
        final["scene_character_card_count"] = len(
            final_context.get("character_cards", [])
            if isinstance(final_context.get("character_cards"), list)
            else []
        )
        return final


def _relationship_prepare_extracted(
    payload: Dict[str, Any],
    *,
    root,
    turn_number: int,
):
    prepared = scene_presence_runtime._apply_presence_contract(deepcopy(payload), root=root)
    prepared = relationship_growth_runtime._merge_footer_delta_fallbacks(prepared, root=root)
    prepared["scene_output"] = relationship_growth_runtime._strip_relationship_footer(
        str(prepared.get("scene_output") or "")
    )
    return runtime_fixes_compat._prepare_extracted_for_commit(
        prepared,
        root=root,
        turn_number=turn_number,
    )


def _prepare_simple_profile_commit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not simple_profile_runtime._simple_session(root):
        return deepcopy(payload)

    simple_profile_runtime._validate_simple_private_input_boundary(root, payload)
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if isinstance(extracted, dict):
        extracted = deepcopy(extracted)
        simple_profile_runtime._prepare_journal_entries(root, extracted)
        simple_profile_runtime._normalize_upserts(root, extracted)
        extracted["turn_knowledge"] = []
        extracted["knowledge_usage"] = []
        extracted["knowledge_trace_complete"] = True
        result["extracted"] = extracted
    return result


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    prepared = _prepare_simple_profile_commit(session_id, payload)

    turn_events = knowledge_firewall_runtime._validate_knowledge_commit(session_id, prepared)
    if turn_events:
        knowledge_firewall_runtime._augment_canon_fill_persistence(prepared, turn_events)

    prepared = relationship_policy_runtime._validate_relationship_commit(session_id, prepared)
    prepared = cast_registry_runtime._with_registry_patch(session_id, prepared)
    scene_logic_runtime._require_knowledge_review(session_id, prepared)

    living_world_runtime._validate_relationship_vocabulary(session_id, prepared)
    prepared, metadata = living_world_runtime._split_relationship_metadata(session_id, prepared)
    prepared = living_world_runtime._with_atomic_state_effects(session_id, prepared, metadata)

    prepared = story_thread_runtime._with_story_patch(session_id, prepared, audit=False)
    prepared = npc_intent_runtime._with_intent_patch(session_id, prepared, audit=False)
    prepared = memory_integrity_runtime._canonicalize_memory_payload(session_id, prepared, audit=False)

    return dict(runtime_fixes.commit_turn(session_id, prepared))


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = story_thread_runtime._with_story_patch(session_id, payload, audit=True)
    prepared = npc_intent_runtime._with_intent_patch(session_id, prepared, audit=True)
    prepared = memory_integrity_runtime._canonicalize_memory_payload(session_id, prepared, audit=True)
    return dict(runtime_fixes.commit_audit(session_id, prepared))


def continue_session(session_id: str) -> Dict[str, Any]:
    stability_runtime._recover_session(session_id)
    result = dict(_BASE_CONTINUE(session_id))
    status = session_recovery.current_recovery_status(session_id)
    result["current_recovery_required"] = bool(status.get("required"))
    if status.get("required"):
        result["current_recovery_reasons"] = status.get("reasons", [])

    root = storage.SESSIONS_DIR / session_id
    result["resume_payload_counts"] = {
        field: resume_compact_runtime._size(result.get(field))
        for field in resume_compact_runtime._HEAVY_RESUME_FIELDS
    }
    for field in resume_compact_runtime._HEAVY_RESUME_FIELDS:
        result.pop(field, None)
    result["resume_payload_compact"] = True
    result["last_committed_turn"] = resume_compact_runtime._last_committed_turn(root)
    pending = resume_compact_runtime._pending_turn(root)
    if pending:
        result["pending_turn"] = pending

    if result.get("current_recovery_required"):
        if pending:
            result["pending_turn_before_current_recovery"] = pending
    elif pending:
        result["instruction"] = (
            "An uncommitted turn packet already exists. last_committed_turn.scene_output is the exact latest committed scene. "
            "Do not start or replace another gameplay turn. Reuse pending_turn with the same request_id, read only unread_chunk_indices, then commit once. "
            "recoverSessionCurrent is not a turn-packet recovery tool."
        )
    else:
        result["instruction"] = (
            "Continue this exact existing session. last_committed_turn.scene_output is the exact latest saved scene and may be shown verbatim when the user asks for the last scene. "
            "The resume response stays compact; full canon remains in persistent storage. On the next gameplay input call prepareTurn for this same session_id."
        )
    return result


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    if simple_profile_runtime._simple_session(storage.SESSIONS_DIR / session_id):
        simple_profile_runtime._ORIGINAL_PARTICIPATION_BUNDLE = _BASE_PARTICIPATION_BUNDLE
        return simple_profile_runtime._participation_bundle(session_id, character_id)
    knowledge_firewall_runtime._ORIGINAL_PARTICIPATION_BUNDLE = _BASE_PARTICIPATION_BUNDLE
    return knowledge_firewall_runtime._strict_participation_bundle(session_id, character_id)


def install() -> None:
    # Relationship compatibility is wired once as plain functions.
    relationship_runtime._parse_footer = runtime_fixes_compat._parse_footer_compat
    relationship_runtime.MAX_DIMENSIONS = relationship_growth_runtime.MAX_RELATIONSHIP_DIMENSIONS
    relationship_runtime._merge_footer_dimensions = relationship_growth_runtime._merge_footer_dimensions

    runtime_fixes._parse_footer = runtime_fixes_compat._parse_footer_compat
    runtime_fixes.relationship_patch_from_scene = runtime_fixes_compat._relationship_patch_from_scene
    runtime_fixes._validate_dimensions = relationship_growth_runtime._validate_dimensions
    runtime_fixes._validate_visible_footer = relationship_growth_runtime._validate_visible_footer
    runtime_fixes._hidden_relationship_scene = relationship_growth_runtime._hidden_relationship_scene
    runtime_fixes._prepare_extracted_for_commit = _relationship_prepare_extracted

    runtime_fixes_compat._validate_dimensions = relationship_growth_runtime._validate_dimensions
    runtime_fixes_compat._validate_visible_footer = relationship_growth_runtime._validate_visible_footer
    runtime_fixes_compat._hidden_relationship_scene = relationship_growth_runtime._hidden_relationship_scene

    # Durable chronology selector, direct once.
    chronology_integrity_runtime._ORIGINAL_SELECT = session_runtime._select_chronology_context
    session_runtime._select_chronology_context = chronology_integrity_runtime._select_chronology_context

    # Public storage/audit compatibility, direct once.
    storage.get_turn_packet_chunk = runtime_fixes.get_turn_packet_chunk
    storage.save_novel = runtime_fixes.save_novel
    storage.get_novel = runtime_fixes.get_novel
    audit_runtime.get_audit_snapshot = fast_audit_runtime.get_audit_snapshot
    audit_runtime.get_audit_snapshot_chunk = runtime_fixes.get_audit_snapshot_chunk
    audit_runtime.require_complete_audit_read = runtime_fixes.require_complete_audit_read
    audit_runtime.clear_audit_packet = runtime_fixes.clear_audit_packet

    # Initial character knowledge seeding without installing the old turn wrapper.
    knowledge_firewall_runtime._ORIGINAL_CREATE_SESSION = _BASE_CREATE_SESSION
    storage.create_session = knowledge_firewall_runtime._create_session

    # Character dossier reads use one explicit dispatcher, not stacked bundle wrappers.
    character_chunk_read._participation_bundle = _participation_bundle

    # Recovery/atomic persistence are single direct replacements.
    stability_runtime._ORIGINAL_RECOVER_CURRENT = _BASE_RECOVER_CURRENT
    session_recovery.recover_session_current = stability_runtime._recover_current
    storage.commit_turn = stability_runtime._atomic_commit_turn
    storage.commit_audit = stability_runtime._atomic_commit_audit

    # The only gameplay surface assignments.
    session_runtime.prepare_turn_packet = prepare_turn_packet
    session_runtime.commit_turn = commit_turn
    session_runtime.commit_audit = commit_audit
    session_runtime.continue_session = continue_session
