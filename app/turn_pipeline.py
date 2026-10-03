from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import (
    audit_runtime,
    cast_registry_runtime,
    character_chunk_read,
    chronology_integrity_runtime,
    fast_audit_runtime,
    game_day,
    knowledge_firewall_runtime,
    knowledge_persistence_runtime,
    location_runtime,
    memory_integrity_runtime,
    npc_intent,
    npc_intent_runtime,
    npc_relationship_runtime,
    private_knowledge_runtime,
    profile_templates,
    relationship_metadata,
    relationship_runtime,
    resume_compact_runtime,
    runtime_fixes,
    runtime_fixes_compat,
    scene_presence_runtime,
    session_recovery,
    session_runtime,
    simple_profile_runtime,
    stability_runtime,
    storage,
    story_thread,
    transport_scope_runtime,
    turn_context,
    writer_first_runtime,
)
from .transactional_storage import session_transaction


_BASE_PREPARE = session_runtime.prepare_turn_packet
_BASE_AUDIT = session_runtime.commit_audit
_BASE_CONTINUE = session_runtime.continue_session
_BASE_PARTICIPATION_BUNDLE = character_chunk_read._participation_bundle
_BASE_CREATE_SESSION = storage.create_session
_BASE_RECOVER_CURRENT = session_recovery.recover_session_current

PIPELINE_VERSION = 13

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
        "chunk_chars_max": writer_first_runtime.WRITER_PACKET_CHARS,
        "first_chunk_included": bool(chunks),
        "reused_pending_packet": reused,
        "read_chunks": read,
        "next_chunk_index": unread[0] if unread else None,
        "all_chunks_read": not unread,
        "turn_pipeline_version": PIPELINE_VERSION,
        "relationship_review_required": bool(packet.get("relationship_review_required")),
        "relationship_review_details_required": bool(packet.get("relationship_review_details_required")),
        "relationship_footer_scope_required": bool(packet.get("relationship_footer_scope_required")),
        "relationship_review_v3_required": bool(packet.get("relationship_review_v3_required")),
        "instruction": (
            "Pending packet reused. Read only unread chunks, silently re-check the final scene against Scene Builder, and commit once."
            if reused
            else "Chunk 0 is included. Read remaining unread chunks, write from Rules + Scene Builder, silently re-check the final scene against Scene Builder, and commit once."
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


def _current_pointer_guard(session_id: str) -> None:
    status = session_recovery.current_recovery_status(session_id)
    if not status.get("required"):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "CURRENT_RECOVERY_REQUIRED",
            "reasons": status.get("reasons", []),
            "instruction": "Repair the technical current scene pointer before preparing another gameplay turn.",
        },
    )


def _strip_legacy_pov_rule_from_session_source(root) -> None:
    source = storage._read_json(root / "source.json", {})
    if not isinstance(source, dict) or not source:
        return
    cleaned = profile_templates._strip_legacy_pov_silence_rule(source)
    if cleaned != source:
        storage._write_json(root / "source.json", cleaned)


def _clear_legacy_audit_gate(root) -> None:
    meta = storage._read_json(root / "meta.json", {})
    if isinstance(meta, dict) and meta.get("audit_required"):
        meta["audit_required"] = False
        storage._write_json(root / "meta.json", meta)


def _scene_ids(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[str]:
    values = [str(value) for value in storage._present_character_ids(state) if value]
    values.extend(str(value) for value in storage._remote_character_ids(state) if value)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        values.insert(0, str(pov["character_id"]))
    valid = {storage._card_id(card) for card in cards if storage._card_id(card)}
    return [cid for cid in dict.fromkeys(values) if cid in valid]


def _cast_registry_rows(
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

    card_map = {
        storage._card_id(card): card
        for card in cards
        if storage._card_id(card)
    }
    runtime = state.get("characters") if isinstance(state.get("characters"), dict) else {}
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = {str(value) for value in storage._present_character_ids(state) if value}
    remote = {str(value) for value in storage._remote_character_ids(state) if value}
    npc_network = npc_relationship_runtime.build_network(
        cards,
        state,
        resolve_character_id=session_runtime._resolve_character_id,
    )

    def compact(value: Any, limit: int = 520) -> str | None:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            text = " ".join(value.split())
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text[:limit] if text else None

    def active_intents(character_id: str) -> List[str]:
        store = state.get("npc_intents")
        rows: Any = []
        if isinstance(store, dict):
            rows = store.get(character_id, [])
        elif isinstance(store, list):
            rows = [
                row for row in store
                if isinstance(row, dict)
                and str(row.get("character_id") or row.get("owner_character_id") or "") == character_id
            ]
        if isinstance(rows, dict):
            rows = list(rows.values())
        result: List[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "active").casefold()
            if status in {"resolved", "closed", "done", "abandoned", "cancelled", "canceled"}:
                continue
            text = (
                row.get("summary")
                or row.get("intent")
                or row.get("goal")
                or row.get("planned_action")
                or row.get("reason")
            )
            if text:
                result.append(" ".join(str(text).split())[:260])
        return result[:4]

    def active_threads(character_id: str) -> List[str]:
        raw = state.get("threads")
        rows = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        result: List[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "active").casefold()
            if status in {"resolved", "closed", "done", "abandoned", "cancelled", "canceled"}:
                continue
            participants = row.get("participants") or row.get("character_ids") or []
            if isinstance(participants, str):
                participants = [participants]
            involved = character_id in {str(value) for value in participants if value}
            if not involved:
                involved = any(
                    str(row.get(key) or "") == character_id
                    for key in ("character_id", "owner_character_id", "target_character_id")
                )
            if not involved:
                continue
            text = row.get("summary") or row.get("title") or row.get("name") or row.get("progress_summary")
            if text:
                result.append(" ".join(str(text).split())[:260])
        return result[:4]

    rows: List[Dict[str, Any]] = []
    for cid, raw in registry.items():
        if not isinstance(raw, dict):
            continue
        card = card_map.get(cid, {})
        info = runtime.get(cid) if isinstance(runtime.get(cid), dict) else {}

        identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
        goals = card.get("goals")
        story_function = (
            raw.get("story_function")
            or card.get("story_function")
            or (goals.get("story_function") if isinstance(goals, dict) else None)
        )
        work = card.get("work") or card.get("occupation") or identity.get("occupation")
        residence = card.get("residence") or card.get("home") or card.get("lives_at")
        relationship_to_pov = relationships.get(cid) if isinstance(relationships.get(cid), dict) else None

        row = {
            "character_id": cid,
            "name": raw.get("name") or storage._card_name(card) or cid,
            "role": raw.get("role") or storage._card_role(card),
            "story_function": compact(story_function, 420),
            "status": raw.get("status") or "active",
            "origin": raw.get("origin"),
            "importance": raw.get("importance"),
            "is_pov": cid == pov_id,
            "present": cid in present,
            "remote": cid in remote,
            "goals": compact(goals),
            "work": compact(work, 320),
            "residence": compact(residence, 320),
            "current_location": compact(info.get("location") or info.get("location_id"), 220),
            "current_zone": compact(info.get("zone") or info.get("zone_id"), 180),
            "current_activity": compact(info.get("activity"), 320),
            "relationship_to_pov": relationship_to_pov,
            "known_relationships": compact(card.get("relationships"), 520),
            "npc_relationships": npc_relationship_runtime.relations_for_character(npc_network, cid),
            "full_card_action": (
                f"prepareCharacterBundleRead(character_id={cid})"
                if cid != pov_id else None
            ),
            "active_intents": active_intents(cid),
            "active_threads": active_threads(cid),
            "last_meaningful_event": raw.get("last_meaningful_event"),
        }
        rows.append({
            key: value
            for key, value in row.items()
            if value not in (None, "", [], {}, False)
        })

    rows.sort(key=lambda row: (
        0 if row.get("is_pov") else 1,
        0 if row.get("origin") == "player_created" else 1,
        {"core": 0, "recurring": 1, "support": 2}.get(str(row.get("importance") or ""), 9),
        str(row.get("name") or row.get("character_id") or "").casefold(),
    ))
    return rows


def _clean_relationship_lens(context: Dict[str, Any]) -> None:
    lens = context.get("relationship_lens")
    if not isinstance(lens, dict):
        return
    lens = deepcopy(lens)
    for key in (
        "instruction",
        "rule",
        "initialization_instruction",
        "initialization_rule",
        "stagnation_rule",
    ):
        lens.pop(key, None)
    candidates = lens.get("present_npc_candidates")
    if isinstance(candidates, list):
        for row in candidates:
            if isinstance(row, dict):
                row.pop("initialization_rule", None)
                row.pop("instruction", None)
                row.pop("rule", None)
    lens["initialization_required"] = False
    context["relationship_lens"] = lens

def _clean_director_layers(context: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)
    for key in (
        "runtime_contract",
        "knowledge_guard",
        "knowledge_boundary",
        "scene_logic_guardrails",
        "knowledge_firewall_v5",
        "dialogue_policy",
        "dialogue_frames",
        "author_only_recollection_context",
        "narrative_guardrails",
        "living_world",
        "relationship_policy",
        "story_pressure",
        "story_drive",
        "foundation_pressure",
        "story_pillar_pressure",
        "cast_pressure",
        "scene_builder_instruction",
        "pov_participation_instruction",
        "npc_agency_instruction",
        "character_context_instruction",
        "relationship_lens_instruction",
        "npc_intent_instruction",
        "simple_knowledge_rules",
        "speaker_context",
        "director_only",
        "character_registry_instruction",
    ):
        result.pop(key, None)

    contract = result.get("working_context_contract")
    contract = deepcopy(contract) if isinstance(contract, dict) else {}
    contract.pop("dormant_character_retrieval", None)
    contract.pop("simple_name_mention_does_not_load_offscreen_card", None)
    contract.update({
        "turn_pipeline_version": PIPELINE_VERSION,
        "director_rules_source": "runtime_rules",
        "scene_rendering_source": "scene_builder",
        "hidden_director_guard_layers": False,
        "backend_semantic_scene_gates": False,
        "precommit_review_gates": ["scene_builder", "persistence", "knowledge", "relationships"],
        "offscreen_character_retrieval": "chunked_when_relevant",
        "active_character_knowledge_rebuilt_from_persistent_memory": True,
    })
    result["working_context_contract"] = contract
    for key in ("novel_rules", "novel", "author_context", "novel_profile"):
        if key in result:
            result[key] = profile_templates._strip_legacy_pov_silence_rule(result[key])
    author = result.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        for key in ("instruction", "knowledge_quarantine", "chronology_context_rule"):
            author.pop(key, None)
        result["author_context"] = author
    _clean_relationship_lens(result)
    return result


def _move_runtime_documents_last(context: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)
    rules = result.pop("runtime_rules", None)
    builder = result.pop("scene_builder", None)
    result["read_order"] = [
        "novel/director context",
        "chronology and recent continuity",
        "current scene state",
        "physical location profile when present",
        "scoped canon notes when relevant",
        "active character cards",
        "each active character's own knowledge",
        "relationships and active intents",
        "cast registry",
        "NPC relationship network",
        "runtime_rules",
        "scene_builder",
    ]
    if rules is not None:
        result["runtime_rules"] = rules
    if builder is not None:
        result["scene_builder"] = builder
    return result


def _prepare_context(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet, context = _read_packet_context(root)
    if not context:
        return base

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0)
    opening_scene = current_turn == 0 and str(packet.get("user_input") or "") == ""
    if opening_scene:
        context["opening_scene"] = {"active": True}

    # session_runtime/turn_context already gives full cards and complete factual knowledge
    # only for POV + physical/remote scene participants. A name mention alone is excluded.
    context = stability_runtime._compact_turn_context(context, source)
    context = transport_scope_runtime._strip_legacy_full_payloads(
        context,
        persistent_state=state,
    )
    context = writer_first_runtime._rewrite_context(session_id, context)
    context = private_knowledge_runtime.redact_private_history(context, root=root, cards=cards)
    context = _clean_director_layers(context)

    scene_ids = _scene_ids(state, cards)
    context["relevant_character_ids"] = scene_ids

    location_context = location_runtime.build_location_context(
        source,
        state,
        scene_character_ids=scene_ids,
    )
    if location_context is not None:
        context["location_context"] = location_context
    else:
        context.pop("location_context", None)

    canon_notes_context = location_runtime.build_canon_notes_context(
        source,
        state,
        scene_character_ids=scene_ids,
    )
    if canon_notes_context is not None:
        context["canon_notes_context"] = canon_notes_context
    else:
        context.pop("canon_notes_context", None)

    card_map = {
        storage._card_id(card): card
        for card in cards
        if storage._card_id(card)
    }
    context["character_cards"] = [
        deepcopy(card_map[cid])
        for cid in scene_ids
        if cid in card_map
    ]
    context["character_profiles"] = {
        cid: profile_templates.render_character_profile(card_map[cid])
        for cid in scene_ids
        if cid in card_map
    }

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    memory_buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    context["character_memory"] = {
        cid: turn_context._working_memory_bucket(memory_buckets.get(cid, {}), current_turn)
        for cid in scene_ids
    }
    npc_network = npc_relationship_runtime.build_network(
        cards,
        state,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    context["cast_registry"] = {
        "persistent": True,
        "registry_index_path": "cast_registry.characters",
        "mandatory_causal_review": True,
        "recency_rotation_disabled": True,
        "instruction": (
            "Перед сценой просмотри ВЕСЬ постоянный NPC-каст и npc_relationship_network. Это не очередь и не ротация. "
            "Давность, число появлений и то, что персонажа давно не было, сами по себе никогда не являются причиной вывести его в сцену. "
            "Для каждого NPC оцени role/story_function, goals, work/residence, current_location/current_activity, "
            "relationship_to_pov, связи NPC↔NPC, active_intents, active_threads и реальные последствия. "
            "Если собственная линия NPC или его связь с другим NPC создаёт причинный путь к текущей/ближайшей сцене, мир сам подводит пересечение. "
            "POV не обязан искать, звать или вспоминать персонажа. Если причины нет, не вставляй NPC ради камео."
        ),
        "characters": _cast_registry_rows(state, cards, source, current_turn),
    }
    context["npc_relationship_network"] = npc_network

    scene_presence = {
        "present_character_ids": [str(value) for value in storage._present_character_ids(state) if value],
        "remote_character_ids": [str(value) for value in storage._remote_character_ids(state) if value],
    }
    context["scene_presence"] = scene_presence

    lens = context.get("relationship_lens")
    if isinstance(lens, dict):
        pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
        pov_id = str(pov.get("character_id") or "")
        physical_ids = [
            str(value) for value in storage._present_character_ids(state)
            if value and str(value) != pov_id
        ]
        remote_ids = [
            str(value) for value in storage._remote_character_ids(state)
            if value and str(value) != pov_id
        ]
        physical_set = set(physical_ids)
        remote_set = set(remote_ids)
        lens["footer_character_ids"] = physical_ids
        lens["remote_participant_ids"] = remote_ids
        relations = lens.get("relations_in_current_scene")
        if isinstance(relations, list):
            for row in relations:
                if not isinstance(row, dict):
                    continue
                owner_id = str(row.get("owner_character_id") or "")
                if owner_id in physical_set:
                    row["participation_mode"] = "physical"
                elif owner_id in remote_set:
                    row["participation_mode"] = "remote"
                last_changed = int(row.get("last_changed_turn", 0) or 0)
                row["turns_since_change"] = max(0, current_turn - last_changed) if last_changed else max(0, current_turn)
                saturated = [
                    str(item.get("label") or item.get("key"))
                    for item in row.get("dimensions", []) if isinstance(item, dict)
                    and isinstance(item.get("value"), (int, float)) and not isinstance(item.get("value"), bool)
                    and float(item.get("value")) >= 100.0
                ]
                if saturated:
                    row["saturated_dimensions"] = saturated
        candidates = lens.get("present_npc_candidates")
        if isinstance(candidates, list):
            lens["present_npc_candidates"] = [
                row for row in candidates
                if isinstance(row, dict) and str(row.get("character_id") or "") in physical_set
            ]
        context["relationship_lens"] = lens

    persistence = context.get("persistence_contract")
    persistence = deepcopy(persistence) if isinstance(persistence, dict) else {}
    persistence.clear()
    persistence.update({
        "rule": "After the scene save only what actually changed. Empty lists are allowed.",
        "chronology": "important durable events only; knowledge_participants is the only chronology field that grants personal knowledge",
        "character_knowledge": (
            "knowledge_journal is personal memory, separate from chronology. Save durable learned facts only to each "
            "character who actually received or learned them; chronology never grants knowledge by itself. Preserve exact granularity: "
            "store the minimum received proposition and never enrich it with an unstated time, place, person, reason, plan or other detail. "
            "A communicated claim may be stored source-qualified (for example, 'Elena said she is not a raider') without declaring it objective author truth."
        ),
        "relationships": (
            "After the complete scene, review every participating NPC->POV relationship and record one relationship_review row per NPC. "
            "If the scene causally changed it, set changed=true and write relationship_updates with a concrete reason. "
            "For an existing metric send delta from the saved value; value may echo the final/snapshot value but delta is authoritative. "
            "A qualitatively new state may become a new dynamic metric with value; old labels are not a whitelist. "
            "If nothing changed, set changed=false and send no update."
        ),
        "npc_relationships": (
            "NPC↔NPC is separate from NPC→POV. If an interaction between two persistent NPCs creates a durable change, "
            "write npc_relationship_updates with owner_character_id, target_character_id and only the qualitative fields that actually changed. "
            "Direction matters; never mirror A→B into B→A automatically."
        ),
        "character_upserts": "new durable self-detail, or a new persistent NPC only with a concrete recurring story_function; never register a background extra",
        "state_patch": "save changed physical state. POV clothing/inventory -> state_patch.pov; NPC clothing/inventory/location/activity -> state_patch.characters[ID]. For a known location preserve location_id and zone_id/zone with the visible location name.",
    })
    context["persistence_contract"] = persistence

    context = _move_runtime_documents_last(context)
    packet = _write_packet_context(root, packet, context)
    result = _packet_manifest(packet, reused=False)
    result["scene_character_card_count"] = len(
        context.get("character_cards", [])
        if isinstance(context.get("character_cards"), list)
        else []
    )
    return result


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    stability_runtime._recover_session(session_id)
    _current_pointer_guard(session_id)
    _clear_legacy_audit_gate(root)
    _strip_legacy_pov_rule_from_session_source(root)
    game_day._sync_session_game_day(session_id)
    knowledge_persistence_runtime.repair_personal_memory_from_safe_chronology(session_id)
    knowledge_persistence_runtime.dedupe_persisted_knowledge_journal(session_id)

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
            if int(pending.get("turn_pipeline_version", 0) or 0) == PIPELINE_VERSION:
                return _packet_manifest(pending, reused=True)

    if (
        isinstance(pending, dict)
        and pending.get("packet_id")
        and int(pending.get("prepared_for_turn", 0) or 0) == expected_turn
        and str(pending.get("user_input") or "") == str(user_input)
        and isinstance(pending.get("chunks"), list)
        and pending.get("chunks")
    ):
        _prepare_context(session_id, _packet_manifest(pending, reused=True))
        refreshed = storage._read_json(root / "turn_packet.json", {})
        return _packet_manifest(refreshed, reused=True)

    base = dict(_BASE_PREPARE(session_id, user_input))
    return _prepare_context(session_id, base)


def _validate_technical_state_patch(payload: Dict[str, Any]) -> None:
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    if "current" not in patch:
        return
    current = patch.get("current")
    if not isinstance(current, dict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CURRENT_STATE_PATCH_INVALID",
                "message": "state_patch.current must stay an object; it cannot erase the current scene pointer.",
            },
        )
    if "present_characters" in current and current.get("present_characters") in (None, "", [], {}):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CURRENT_STATE_PATCH_INVALID",
                "message": "current.present_characters cannot be cleared.",
            },
        )


def _dynamic_merge_dimensions(existing: Any, incoming: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    by_norm: Dict[str, int] = {}

    for raw in existing if isinstance(existing, list) else []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip()
        value = raw.get("value")
        if not label or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        key = str(raw.get("key") or relationship_runtime._dimension_key(label))
        norm = relationship_runtime._norm(label)
        if norm in by_norm:
            continue
        by_norm[norm] = len(result)
        result.append({"key": key, "label": label, "value": value})

    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("key") or "").strip()
        if not label:
            continue
        norm = relationship_runtime._norm(label)
        old_value = result[by_norm[norm]]["value"] if norm in by_norm else None
        value = raw.get("value")
        delta = raw.get("delta")
        value_is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        delta_is_number = isinstance(delta, (int, float)) and not isinstance(delta, bool)

        # Compatibility rule:
        # - established metrics use delta when it is supplied, so a stale snapshot value cannot cancel change;
        # - legacy absolute writes without delta still work;
        # - a new metric needs an absolute value because it has no saved baseline.
        if old_value is not None:
            if delta_is_number:
                final = old_value + delta
            elif value_is_number:
                final = value
            else:
                continue
        elif value_is_number:
            final = value
        else:
            continue

        if norm in by_norm:
            result[by_norm[norm]]["value"] = final
        else:
            by_norm[norm] = len(result)
            result.append({
                "key": str(raw.get("key") or relationship_runtime._dimension_key(label)),
                "label": label,
                "value": final,
            })
    return result


def _apply_relationship_changes(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)

    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    turns = storage._read_turns(root)
    state_before = storage._read_json(root / "state.json", {})
    state_before = relationship_runtime.repair_relationship_state(
        state_before,
        source=source,
        turns=turns,
        cards=cards,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    state_patch = deepcopy(extracted.get("state_patch")) if isinstance(extracted.get("state_patch"), dict) else {}
    state_after = storage._deep_merge(state_before, state_patch)

    docs = relationship_runtime._canonical_docs(
        state_before,
        cards=cards,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = set(storage._present_character_ids(state_after))

    packet = storage._read_json(root / "turn_packet.json", {})
    footer_is_display_only = bool(packet.get("relationship_review_v3_required"))
    footer = {} if footer_is_display_only else runtime_fixes_compat._parse_footer_compat(
        str(result.get("scene_output") or ""),
        cards=cards,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    relationship_updates = (
        extracted.get("relationship_updates")
        if isinstance(extracted.get("relationship_updates"), list)
        else []
    )
    if not footer and not relationship_updates:
        return result

    # The visible footer is a compatibility fallback. If the same owner/metric has an explicit
    # relationship_update, do not apply the footer copy first or a delta would be counted twice.
    explicit_labels: Dict[str, set[str]] = {}
    for raw in relationship_updates:
        if not isinstance(raw, dict):
            continue
        owner_id = session_runtime._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            continue
        owner_key = str(owner_id)
        for item in raw.get("dimensions", []) if isinstance(raw.get("dimensions"), list) else []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("key") or "").strip()
            if label:
                explicit_labels.setdefault(owner_key, set()).add(relationship_runtime._norm(label))

    for owner_id, dimensions in footer.items():
        owner_id = str(owner_id)
        if not owner_id or owner_id == pov_id or owner_id not in present:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = relationship_runtime._pov_relation(doc, pov_id)
        if relation is None:
            relation = relationship_runtime._empty_relation(pov_id, [])
            doc["relations"].append(relation)
        blocked = explicit_labels.get(owner_id, set())
        fallback_dimensions = [
            item for item in dimensions
            if isinstance(item, dict)
            and relationship_runtime._norm(item.get("label") or item.get("key")) not in blocked
        ]
        relation["dimensions"] = _dynamic_merge_dimensions(relation.get("dimensions"), fallback_dimensions)

    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1
    metadata_rows: List[Dict[str, Any]] = []
    for raw in relationship_updates:
        if not isinstance(raw, dict):
            continue
        owner_id = session_runtime._resolve_character_id(cards, raw.get("character_id"))
        if not owner_id or str(owner_id) == pov_id:
            continue
        owner_id = str(owner_id)
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relation = relationship_runtime._pov_relation(doc, pov_id)
        if relation is None:
            relation = relationship_runtime._empty_relation(pov_id, [])
            doc["relations"].append(relation)

        before = {
            relationship_runtime._norm(item.get("label")): item.get("value")
            for item in relation.get("dimensions", [])
            if isinstance(item, dict)
        }
        incoming = raw.get("dimensions") if isinstance(raw.get("dimensions"), list) else []
        relation["dimensions"] = _dynamic_merge_dimensions(relation.get("dimensions"), incoming)
        numeric_changes = []
        for item in relation.get("dimensions", []):
            norm = relationship_runtime._norm(item.get("label"))
            if norm in before and before[norm] != item.get("value"):
                numeric_changes.append({
                    "label": item.get("label"),
                    "from": before[norm],
                    "to": item.get("value"),
                })
            elif norm not in before:
                numeric_changes.append({
                    "label": item.get("label"),
                    "from": None,
                    "to": item.get("value"),
                })
        meta_row = deepcopy(raw)
        meta_row["character_id"] = owner_id
        meta_row["_numeric_changes"] = numeric_changes
        metadata_rows.append(meta_row)
        if numeric_changes:
            relation["last_changed_turn"] = turn_number

    synced = relationship_runtime._sync_state(state_after, docs)
    synced, _ = relationship_metadata.apply_relationship_metadata(
        synced,
        metadata_rows,
        turn_number=turn_number,
    )

    if synced.get("relationships", {}) != state_after.get("relationships", {}):
        state_patch["relationships"] = deepcopy(synced.get("relationships", {}))
    if synced.get("relationship_documents", {}) != state_after.get("relationship_documents", {}):
        state_patch["relationship_documents"] = deepcopy(synced.get("relationship_documents", {}))
    state_patch.pop("relationship_schemas", None)
    extracted["state_patch"] = state_patch
    result["extracted"] = extracted
    return result


def _apply_story_and_intent_updates(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    patch = deepcopy(extracted.get("state_patch")) if isinstance(extracted.get("state_patch"), dict) else {}
    working = storage._deep_merge(state, patch)
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1

    thread_updates = extracted.get("story_thread_updates")
    if isinstance(thread_updates, list) and thread_updates:
        working = story_thread.apply_updates(working, thread_updates, current_turn=turn_number)
        patch["threads"] = deepcopy(working.get("threads", {}))

    intent_updates = extracted.get("npc_intent_updates")
    if isinstance(intent_updates, list) and intent_updates:
        # Keep only the narrow provenance check from the legacy intent adapter.
        # Do not reinstall its gameplay wrapper.
        npc_intent_runtime._validate_intent_sources(root, extracted, intent_updates)
        working = npc_intent.apply_updates(working, intent_updates, current_turn=turn_number)
        patch["npc_intents"] = deepcopy(working.get("npc_intents", {}))

    extracted["state_patch"] = patch
    result["extracted"] = extracted
    return result


def _apply_npc_relationship_updates(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)
    updates = extracted.get("npc_relationship_updates")
    if not isinstance(updates, list) or not updates:
        return result

    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    patch = deepcopy(extracted.get("state_patch")) if isinstance(extracted.get("state_patch"), dict) else {}
    working = storage._deep_merge(state, patch)
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1

    try:
        working = npc_relationship_runtime.apply_updates(
            working,
            updates,
            cards=cards,
            resolve_character_id=session_runtime._resolve_character_id,
            turn_number=turn_number,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": str(exc), "message": "Invalid NPC-to-NPC relationship update."},
        ) from exc

    patch["npc_relationships"] = deepcopy(working.get("npc_relationships", {}))
    extracted["state_patch"] = patch
    result["extracted"] = extracted
    return result


def _prepare_profile_persistence(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        extracted = {}
    extracted = deepcopy(extracted)

    extracted.setdefault("chronology", [])
    extracted.setdefault("knowledge_add", [])
    extracted.setdefault("experiences_add", [])
    extracted.setdefault("dialogue_memory_add", [])
    extracted.setdefault("knowledge_journal_add", [])
    extracted.setdefault("npc_intent_updates", [])
    extracted.setdefault("npc_relationship_updates", [])
    extracted.setdefault("story_thread_updates", [])
    extracted.setdefault("relationship_updates", [])
    extracted.setdefault("character_upserts", [])
    extracted.setdefault("presence_updates", [])
    extracted.setdefault("state_patch", {})

    if simple_profile_runtime._simple_session(root):
        simple_profile_runtime._prepare_journal_entries(root, extracted)
        simple_profile_runtime._normalize_upserts(root, extracted)

    result["extracted"] = extracted
    return result


def _normalise_chronology_for_save(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)

    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    post_state = storage._deep_merge(state, patch)
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1

    extracted["chronology"] = session_runtime._normalise_chronology_events(
        extracted.get("chronology", []),
        turn_number=turn_number,
        state=post_state,
        cards=cards,
    )
    result["extracted"] = extracted
    return result


def _disable_mandatory_audit_after_commit(session_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    meta = storage._read_json(root / "meta.json", {})
    if isinstance(meta, dict) and meta.get("audit_required"):
        meta["audit_required"] = False
        storage._write_json(root / "meta.json", meta)
    updated = dict(result)
    updated["audit_due"] = False
    updated["audit_range"] = None
    updated["audit_required"] = False
    return updated


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_technical_state_patch(payload)
    prepared = _prepare_profile_persistence(session_id, payload)
    private_knowledge_runtime.validate_private_knowledge(session_id, prepared)
    prepared = private_knowledge_runtime.add_direct_communication_memory(session_id, prepared)
    prepared = private_knowledge_runtime.add_scene_remote_communication_memory(session_id, prepared)
    prepared = scene_presence_runtime._apply_presence_contract(
        prepared,
        root=storage.SESSIONS_DIR / session_id,
    )
    prepared = _apply_story_and_intent_updates(session_id, prepared)
    prepared = _apply_npc_relationship_updates(session_id, prepared)
    prepared = _apply_relationship_changes(session_id, prepared)
    prepared = cast_registry_runtime._with_registry_patch(session_id, prepared)
    raw_chronology = deepcopy(
        prepared.get("extracted", {}).get("chronology", [])
        if isinstance(prepared.get("extracted"), dict)
        else []
    )
    prepared = _normalise_chronology_for_save(session_id, prepared)
    prepared = knowledge_persistence_runtime.attach_explicit_chronology_participants(
        session_id,
        prepared,
        raw_chronology,
    )
    prepared = knowledge_persistence_runtime.mirror_explicit_chronology_to_personal_memory(
        session_id,
        prepared,
    )
    prepared = knowledge_persistence_runtime.dedupe_new_journal_against_persisted(
        session_id,
        prepared,
    )
    prepared = memory_integrity_runtime._canonicalize_memory_payload(
        session_id,
        prepared,
        audit=False,
    )

    saved = dict(stability_runtime._atomic_commit_turn(session_id, prepared))
    saved = _disable_mandatory_audit_after_commit(session_id, saved)
    saved["saved_chronology_events"] = len(
        prepared.get("extracted", {}).get("chronology", [])
        if isinstance(prepared.get("extracted"), dict)
        else []
    )
    saved["turn_pipeline_version"] = PIPELINE_VERSION
    return saved


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Audit remains available as an optional maintenance tool, but gameplay no longer blocks on it.
    prepared = memory_integrity_runtime._canonicalize_memory_payload(
        session_id,
        payload,
        audit=True,
    )
    return dict(stability_runtime._atomic_commit_audit(session_id, prepared))


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
            "An uncommitted turn packet already exists. Reuse pending_turn and commit it once. "
            "recoverSessionCurrent is not a turn-packet recovery tool."
        )
    else:
        result["instruction"] = (
            "Continue this exact session. last_committed_turn.scene_output is the exact latest saved scene. "
            "On the next gameplay input call prepareTurn for this same session_id."
        )
    return result


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    if simple_profile_runtime._simple_session(storage.SESSIONS_DIR / session_id):
        simple_profile_runtime._ORIGINAL_PARTICIPATION_BUNDLE = _BASE_PARTICIPATION_BUNDLE
        return simple_profile_runtime._participation_bundle(session_id, character_id)
    return dict(_BASE_PARTICIPATION_BUNDLE(session_id, character_id))


def install() -> None:
    # Formatting compatibility only. This parser does not impose relationship semantics.
    relationship_runtime._parse_footer = runtime_fixes_compat._parse_footer_compat
    relationship_runtime._merge_footer_dimensions = _dynamic_merge_dimensions

    # Durable chronology selection is data retrieval, not directing.
    chronology_integrity_runtime._ORIGINAL_SELECT = session_runtime._select_chronology_context
    session_runtime._select_chronology_context = chronology_integrity_runtime._select_chronology_context

    # Safe public storage helpers.
    storage.get_turn_packet_chunk = runtime_fixes.get_turn_packet_chunk
    storage.save_novel = runtime_fixes.save_novel
    storage.get_novel = runtime_fixes.get_novel

    # Optional audit endpoints remain readable, but gameplay never blocks on them.
    audit_runtime.get_audit_snapshot = fast_audit_runtime.get_audit_snapshot
    audit_runtime.get_audit_snapshot_chunk = runtime_fixes.get_audit_snapshot_chunk
    audit_runtime.require_complete_audit_read = runtime_fixes.require_complete_audit_read
    audit_runtime.clear_audit_packet = runtime_fixes.clear_audit_packet

    # Initial dedicated knowledge seeding and on-demand dossier reads are data plumbing only.
    knowledge_firewall_runtime._ORIGINAL_CREATE_SESSION = _BASE_CREATE_SESSION
    storage.create_session = knowledge_firewall_runtime._create_session
    character_chunk_read._participation_bundle = _participation_bundle

    stability_runtime._ORIGINAL_RECOVER_CURRENT = _BASE_RECOVER_CURRENT
    session_recovery.recover_session_current = stability_runtime._recover_current

    session_runtime.prepare_turn_packet = prepare_turn_packet
    session_runtime.commit_turn = commit_turn
    session_runtime.commit_audit = commit_audit
    session_runtime.continue_session = continue_session
