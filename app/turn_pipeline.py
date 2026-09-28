from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import (
    cast_registry_runtime,
    game_day,
    npc_intent,
    profile_templates,
    relationship_policy_runtime,
    scene_presence_runtime,
    session_recovery,
    session_runtime,
    simple_profile_runtime,
    stability_runtime,
    storage,
    writer_first_runtime,
)
from .story_thread import apply_updates as apply_story_thread_updates
from .transactional_storage import session_transaction


# Capture the real core functions once. No runtime is allowed to wrap these later.
_BASE_PREPARE = session_runtime.prepare_turn_packet
_BASE_COMMIT = session_runtime.commit_turn
_BASE_AUDIT = session_runtime.commit_audit
_BASE_CONTINUE = session_runtime.continue_session

PIPELINE_VERSION = 1


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
    chunks = [text[i:i + size] for i in range(0, len(text), size)] or ["{}"]
    packet = deepcopy(packet)
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    # Chunk 0 is returned inline by the manifest.
    packet["read_chunks"] = [0]
    packet["turn_pipeline_version"] = PIPELINE_VERSION
    storage._write_json(root / "turn_packet.json", packet)
    return packet


def _scene_roster_context(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    present = scene_presence_runtime._present_ids(cards, state)
    remote = scene_presence_runtime._remote_ids(cards, state)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    return {
        "pov_character_id": pov_id or None,
        "present_character_ids": present,
        "remote_character_ids": remote,
        "participant_character_ids": list(dict.fromkeys([*present, *remote])),
        "rule": (
            "Physical presence, remote contact and mere mention are different. "
            "Only present/remote participants need active character context. "
            "Mentioning an offscreen person does not give them scene knowledge."
        ),
    }


def _cast_index(
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    source: Dict[str, Any],
    current_turn: int,
) -> List[Dict[str, Any]]:
    source_ids = {
        storage._card_id(card)
        for card in storage._normalise_cards(source.get("characters", []))
        if storage._card_id(card)
    }
    registry = cast_registry_runtime._ensure_registry(
        state,
        cards,
        current_turn,
        source_character_ids=source_ids,
        source=source,
    )
    # Registry is informational only here. No hidden rotation thresholds or forced entrances.
    return cast_registry_runtime._registry_index(registry)


def _relationship_index(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    return relationship_policy_runtime._relationship_index(state, cards)


def _simple_profile_context(
    context: Dict[str, Any],
    *,
    root,
    state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    source: Dict[str, Any],
) -> Dict[str, Any]:
    if not simple_profile_runtime._simple_session(root):
        return context

    result = deepcopy(context)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    roster = result.get("scene_roster") if isinstance(result.get("scene_roster"), dict) else {}
    ids = [
        str(value)
        for value in roster.get("participant_character_ids", [])
        if value
    ]
    if pov_id and pov_id not in ids:
        ids.insert(0, pov_id)

    for key in (
        "knowledge_firewall_v5",
        "dialogue_policy",
        "dialogue_frames",
        "author_only_recollection_context",
        "knowledge_guard",
        "character_cards",
        "scene_characters",
        "character_knowledge",
        "knowledge_journals",
    ):
        result.pop(key, None)

    result["novel_profile"] = profile_templates.render_novel_profile(source.get("novel", {}))
    result["character_profiles"] = simple_profile_runtime._profile_map(cards, ids)
    result["speaker_context"] = simple_profile_runtime._speaker_context(ids, pov_id)
    result["director_only"] = {
        "hidden_lore": profile_templates.render_hidden_lore(source.get("hidden_lore", {})),
        "rule": (
            "Novel profile, hidden lore, chronology and scene history are director knowledge. "
            "They never become POV/NPC knowledge by themselves."
        ),
    }
    return result


def _lean_persistence_contract(context: Dict[str, Any]) -> None:
    context["persistence_contract"] = {
        "required": True,
        "rule": (
            "After the scene save only what actually changed. "
            "Do not invent chronology, knowledge, relationship, intent or cast updates just to fill fields."
        ),
        "required_fields": {
            "persistence_reviewed": True,
            "chronology": "list, may be empty",
            "knowledge_add": "list, may be empty",
            "experiences_add": "list, may be empty",
            "dialogue_memory_add": "list, may be empty",
        },
        "optional_fields": [
            "knowledge_journal_add",
            "relationship_updates",
            "npc_intent_updates",
            "story_thread_updates",
            "presence_updates",
            "character_upserts",
            "state_patch",
        ],
    }


def _clean_context(context: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)
    # These used to be separate mandatory runtime layers. The author rules and scene builder
    # are now the single behavioral contract, so old duplicated policy blocks are removed.
    for key in (
        "narrative_guardrails",
        "living_world",
        "scene_logic_guardrails",
        "knowledge_firewall_v5",
        "dialogue_policy",
        "dialogue_frames",
        "relationship_policy",
        "story_drive",
        "story_pressure",
        "cast_pressure",
    ):
        result.pop(key, None)
    return result


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    # Recovery and game clock are explicit pre-steps, not nested wrappers.
    stability_runtime._recover_session(session_id)
    game_day._sync_session_game_day(session_id)

    base = dict(_BASE_PREPARE(session_id, user_input))

    with session_transaction(root):
        packet, context = _read_packet_context(root)
        if not context:
            return base

        context = writer_first_runtime._rewrite_context(session_id, context)
        context = _clean_context(context)

        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        state = storage._read_json(root / "state.json", {})
        meta = storage._read_json(root / "meta.json", {})
        current_turn = int(meta.get("turn_number", 0) or 0)

        context["scene_roster"] = _scene_roster_context(state, cards)
        context["character_registry"] = _cast_index(state, cards, source, current_turn)
        context["relationship_index"] = _relationship_index(state, cards)
        context["npc_active_intents"] = npc_intent.active_intents_for(
            state,
            context["scene_roster"]["participant_character_ids"],
            current_turn=current_turn,
        )
        context = _simple_profile_context(
            context,
            root=root,
            state=state,
            cards=cards,
            source=source,
        )
        _lean_persistence_contract(context)

        contract = context.get("working_context_contract")
        contract = deepcopy(contract) if isinstance(contract, dict) else {}
        contract.update({
            "turn_pipeline_version": PIPELINE_VERSION,
            "behavior_contract": ["runtime_rules", "scene_builder"],
            "hidden_runtime_policy_layers": False,
            "forced_pov_activity_quota": False,
            "forced_story_progress_after_n_turns": False,
            "forced_cast_rotation_thresholds": False,
        })
        context["working_context_contract"] = contract

        packet = _write_packet_context(root, packet, context)
        result = writer_first_runtime._manifest(packet, base)
        result["turn_pipeline_version"] = PIPELINE_VERSION
        result["instruction"] = (
            "Chunk 0 is included. Read remaining chunks. "
            "Behavior comes from runtime_rules and scene_builder only; packet data provides canon, "
            "current participants, their profiles/knowledge, relationships, intents and continuity."
        )
        return result


def _apply_story_thread_updates(session_id: str, payload: Dict[str, Any], *, audit: bool) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + (0 if audit else 1)
    key = "repairs" if audit else "extracted"
    container = result.get(key) if isinstance(result.get(key), dict) else {}
    updates = container.get("story_thread_updates")
    if not isinstance(updates, list) or not updates:
        return result

    updated_state = apply_story_thread_updates(state, updates, current_turn=turn_number)
    patch = deepcopy(container.get("state_patch")) if isinstance(container.get("state_patch"), dict) else {}
    patch["threads"] = deepcopy(updated_state.get("threads", {}))
    container = deepcopy(container)
    container["state_patch"] = patch
    result[key] = container
    return result


def _prepare_simple_profile_commit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not simple_profile_runtime._simple_session(root):
        return deepcopy(payload)

    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        return result
    extracted = deepcopy(extracted)
    simple_profile_runtime._prepare_journal_entries(root, extracted)
    simple_profile_runtime._normalize_upserts(root, extracted)
    # Old claim ledgers are intentionally not required by the lean pipeline.
    extracted.pop("knowledge_usage", None)
    extracted.pop("turn_knowledge", None)
    extracted.pop("knowledge_trace_complete", None)
    result["extracted"] = extracted
    return result


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # One explicit sequence. No wrapper calls another wrapper.
    prepared = _prepare_simple_profile_commit(session_id, payload)
    prepared = scene_presence_runtime._apply_presence_contract(
        prepared,
        root=storage.SESSIONS_DIR / session_id,
    )
    prepared = cast_registry_runtime._with_registry_patch(session_id, prepared)
    prepared = _apply_story_thread_updates(session_id, prepared, audit=False)
    return dict(_BASE_COMMIT(session_id, prepared))


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = _apply_story_thread_updates(session_id, payload, audit=True)
    return dict(_BASE_AUDIT(session_id, prepared))


def continue_session(session_id: str) -> Dict[str, Any]:
    stability_runtime._recover_session(session_id)
    return dict(_BASE_CONTINUE(session_id))


def install() -> None:
    # One assignment per public turn surface. No nested monkey-patch inheritance.
    session_runtime.prepare_turn_packet = prepare_turn_packet
    session_runtime.commit_turn = commit_turn
    session_runtime.commit_audit = commit_audit
    session_runtime.continue_session = continue_session

    # Preserve the proven atomic storage writer and recovery, but wire them directly once.
    storage.commit_turn = stability_runtime._atomic_commit_turn
    storage.commit_audit = stability_runtime._atomic_commit_audit
    session_recovery.recover_session_current = stability_runtime._recover_current
