from __future__ import annotations

import json
import re
import secrets
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
    knowledge_persistence_runtime,
    location_runtime,
    memory_integrity_runtime,
    npc_intent,
    npc_intent_runtime,
    npc_relationship_runtime,
    private_knowledge_runtime,
    profile_templates,
    relationship_file_runtime,
    relationship_review_runtime,
    resume_compact_runtime,
    runtime_access,
    runtime_fixes,
    scene_presence_runtime,
    scene_knowledge_guard,
    scene_progression_runtime,
    session_migrations,
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

PIPELINE_VERSION = 21

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
        "relationship_review_required": bool(packet.get("relationship_review_required")),
        "progression_review_required": bool(packet.get("progression_review_required")),
        "chunk_chars_max": writer_first_runtime.WRITER_PACKET_CHARS,
        "first_chunk_included": bool(chunks),
        "reused_pending_packet": reused,
        "read_chunks": read,
        "next_chunk_index": unread[0] if unread else None,
        "all_chunks_read": not unread,
        "turn_pipeline_version": PIPELINE_VERSION,
        "runtime_revision": packet.get("runtime_revision"),
        "data_schema_version": int(packet.get("data_schema_version", 0) or 0),
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
    packet["runtime_revision"] = runtime_access.runtime_revision()
    packet["data_schema_version"] = session_migrations.CURRENT_DATA_SCHEMA_VERSION
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
    # The archived author input must remain verbatim, including whitespace and
    # old wording. Only the working canon receives the legacy-rule cleanup.
    working_source = {key: value for key, value in source.items() if key != "source_intake"}
    cleaned = profile_templates._strip_legacy_pov_silence_rule(working_source)
    if "source_intake" in source:
        cleaned["source_intake"] = deepcopy(source["source_intake"])
    if cleaned != source:
        storage._write_json(root / "source.json", cleaned)


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
    npc_network: Dict[str, Any],
    relationship_store: Dict[str, Any],
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
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = {str(value) for value in storage._present_character_ids(state) if value}
    remote = {str(value) for value in storage._remote_character_ids(state) if value}
    def compact(value: Any, limit: int = 140) -> str | None:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            text = " ".join(value.split())
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text[:limit] if text else None

    def active_intents(character_id: str) -> List[str]:
        scoped = npc_intent.active_intents_for(
            state,
            [character_id],
            current_turn=current_turn,
        )
        rows = scoped.get(character_id, []) if isinstance(scoped, dict) else []
        result: List[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("eligible_now") is not True:
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

        goals = card.get("goals")
        story_function = (
            raw.get("story_function")
            or card.get("story_function")
            or (goals.get("story_function") if isinstance(goals, dict) else None)
        )
        relation = relationship_file_runtime.character_relation(relationship_store, cid)
        relation_dims = relation.get("dimensions") if isinstance(relation, dict) and isinstance(relation.get("dimensions"), dict) else {}
        pov_relationship = {
            str(label): item.get("value")
            for label, item in relation_dims.items()
            if isinstance(item, dict) and item.get("value") not in (None, 0)
        }
        row = {
            "character_id": cid,
            "name": raw.get("name") or storage._card_name(card) or cid,
            "role": raw.get("role") or storage._card_role(card),
            "story_function": compact(story_function, 180),
            "status": raw.get("status") or "active",
            "origin": raw.get("origin"),
            "importance": raw.get("importance"),
            "is_pov": cid == pov_id,
            "present": cid in present,
            "remote": cid in remote,
            "story_actor": cid != pov_id and not cast_registry_runtime._is_inactive(raw.get("status") or info.get("status") or card.get("status")),
            "initiative_eligible": cid != pov_id and not cast_registry_runtime._is_inactive(raw.get("status") or info.get("status") or card.get("status")),
            "offscreen_can_initiate": cid != pov_id and cid not in present and cid not in remote and not cast_registry_runtime._is_inactive(raw.get("status") or info.get("status") or card.get("status")),
            "goals": compact(goals),
            "work": compact(card.get("work"), 120),
            "habits": compact(card.get("habits"), 150),
            "character": compact(card.get("character") or card.get("personality"), 150),
            "pov_relationship": pov_relationship or None,
            "pov_relationship_dynamic": compact(relation.get("dynamic"), 320) if isinstance(relation, dict) else None,
            "current_location": compact(info.get("location") or info.get("location_id"), 140),
            "current_zone": compact(info.get("zone") or info.get("zone_id"), 100),
            "current_activity": compact(info.get("activity"), 180),
            "pov_familiarity": deepcopy(info.get("pov_familiarity")) if isinstance(info.get("pov_familiarity"), dict) else None,
            "npc_relation_refs": npc_relationship_runtime.relation_refs_for_character(npc_network, cid),
            "active_intents": active_intents(cid),
            "active_threads": active_threads(cid),
            "last_contact_turn": raw.get("last_contact_turn"),
            "last_contact_game_day": raw.get("last_contact_game_day"),
            "last_contact_mode": raw.get("last_contact_mode"),
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


def _independent_cast_focus(rows: List[Dict[str, Any]], current_turn: int) -> List[Dict[str, Any]]:
    """Expose every independent active NPC, regardless of POV attention or intents."""
    candidates = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("offscreen_can_initiate") is True
        and row.get("character_id")
    ]
    candidates.sort(key=lambda row: (
        {"core": 0, "recurring": 1, "support": 2}.get(str(row.get("importance") or ""), 3),
        str(row.get("character_id")),
    ))
    # All active offscreen NPCs are included, even without a goal or prior intent.
    # Registry rows already carry the full compact profile; never duplicate
    # their text in the turn packet just to highlight cast agency.
    return [{
        "character_id": row["character_id"],
        "name": row.get("name"),
    } for row in candidates]


def _clean_relationship_lens(context: Dict[str, Any]) -> None:
    lens = context.get("relationship_lens")
    if not isinstance(lens, dict):
        return
    lens = deepcopy(lens)
    for key in (
        "instruction",
        "rule",
        "initialization_instruction",
        "stagnation_rule",
    ):
        lens.pop(key, None)
    candidates = lens.get("present_npc_candidates")
    if isinstance(candidates, list):
        for row in candidates:
            if isinstance(row, dict):
                row.pop("instruction", None)
                row.pop("rule", None)
    lens.pop("initialization_required", None)
    lens["initialization_rule"] = (
        "После первого содержательного взаимодействия постоянного NPC с POV сохрани реальный показатель через relationship_updates."
    )
    lens["small_shift_rule"] = (
        "Не жди крупного события ради обычного изменения: ±1 = небольшой, но реальный сдвиг; "
        "±2 = ясный сдвиг; ±3 = сильный обычный сдвиг. >3 только critical_event."
    )
    context["relationship_lens"] = lens

def _clean_director_layers(context: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)

    # relationships.json is the sole relationship canon. Base compatibility
    # builders may still have copied legacy relationship stores into the packet
    # before migration runs, especially on the first turn of an old session.
    legacy_relationship_keys = (
        "relationships",
        "relationship_documents",
        "relationship_schemas",
        "npc_relationships",
    )
    for key in legacy_relationship_keys:
        result.pop(key, None)
    scene_state = result.get("scene_state")
    if isinstance(scene_state, dict):
        scene_state = deepcopy(scene_state)
        for key in legacy_relationship_keys:
            scene_state.pop(key, None)
        result["scene_state"] = scene_state

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
        "character_registry",
        "scene_characters",
        "working_context_contract",
        "chronology_policy",
        "transport_context_paths",
    ):
        result.pop(key, None)

    for key in ("novel_rules", "novel", "author_context", "novel_profile"):
        if key in result:
            result[key] = profile_templates._strip_legacy_pov_silence_rule(result[key])
    author = result.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        for key in ("instruction", "knowledge_quarantine", "chronology_context_rule", *legacy_relationship_keys):
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
        "explicit NPC knowledge boundaries",
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


def _prepare_context(
    session_id: str,
    base: Dict[str, Any],
    *,
    packet_override: Dict[str, Any] | None = None,
    context_override: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if packet_override is None or context_override is None:
        packet, context = _read_packet_context(root)
    else:
        packet = deepcopy(packet_override)
        context = deepcopy(context_override)
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
    # cast_registry below is the single always-read cast index. Remove the older
    # writer-facing cast_index so recency metadata cannot compete with causal selection.
    context.pop("cast_index", None)

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
    # character_cards is the single lossless active-card representation.
    # Do not render the same cards a second time into character_profiles.

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    memory_buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    context["character_memory"] = {
        cid: turn_context._working_memory_bucket(
            memory_buckets.get(cid, {}),
            current_turn,
            character_id=cid,
            cards=cards,
        )
        for cid in scene_ids
    }
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    context["knowledge_boundaries"] = scene_knowledge_guard.build_boundaries(
        context,
        scene_ids,
        pov_id=str(pov.get("character_id") or ""),
    )
    relationship_store = relationship_file_runtime.load(
        root,
        cards=cards,
        state=state,
        pov_id=str(pov.get("character_id") or ""),
    )
    npc_network = relationship_file_runtime.npc_network(relationship_store)
    source_ids = {
        storage._card_id(card)
        for card in storage._normalise_cards(source.get("characters", []))
        if storage._card_id(card)
    }
    context["cast_registry"] = {
        "persistent": True,
        "active_story_cast": True,
        "registry_index_path": "cast_registry.characters",
        "mandatory_causal_review": True,
        "review_every_turn": True,
        "offscreen_bundle_read": {
            "action": "prepareCharacterBundleRead",
            "then": "read all getCharacterBundleChunk chunks before material participation",
            "rule": (
                "Cast registry already establishes eligibility. Do not use bundle reading as a permission gate or reason to postpone an NPC. "
                "Choose initiative from the compact row; once an offscreen NPC is going to participate, read the full bundle before substantive participation."
            ),
        },
        "instruction": (
            "Перед каждой сценой просмотри весь постоянный NPC-каст и npc_relationship_network. "
            "Каждый active NPC в cast_registry — действующая фигура истории, а не справочная запись. "
            "Не жди приглашения POV, отдельного intent/thread, удобной сцены, таймера отсутствия или специальной причины. "
            "Собственная цель, характер, отношения, работа, привычки, текущие дела и возможность действовать уже достаточны для инициативы. "
            "Если offscreen NPC может прийти, пересечься, вмешаться, позвонить, написать или иначе действовать в рамках себя — не откладывай его только ради сохранения текущей сцены. "
            "До содержательного участия выбранного offscreen NPC прочитай его полный character bundle; это проверка знания, а не разрешение на появление."
        ),
        "characters": _cast_registry_rows(state, cards, source, current_turn, npc_network, relationship_store),
    }
    context["cast_registry"]["independent_initiative_instruction"] = (
        "Каждый ход анализируй весь active offscreen cast независимо от упоминаний POV и наличия intents. "
        "Самостоятельные действия следуют из доступных NPC знаний, характера, работы, целей и обстоятельств. "
        "Дай им влиять на мир, когда причинно возможно, без искусственного появления и без обязательной ротации."
    )
    context["npc_relationship_network"] = npc_network
    # Legacy intent-only candidate list falsely implied that offscreen NPCs
    # without a pre-existing intent were ineligible to act. The complete
    # cast_registry is now the single offscreen review surface.
    context.pop("offscreen_intent_candidates", None)

    scene_presence = {
        "present_character_ids": [str(value) for value in storage._present_character_ids(state) if value],
        "remote_character_ids": [str(value) for value in storage._remote_character_ids(state) if value],
    }
    context["scene_presence"] = scene_presence
    context["progression_contract"] = scene_progression_runtime.build_contract(
        state=state,
        context=context,
        user_input=str(packet.get("user_input") or ""),
        cards=cards,
        current_turn=current_turn,
    )

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
        lens["review_required_character_ids_at_scene_start"] = physical_ids
        lens["review_scope"] = (
            "Review every NPC who physically participates at any point in the completed scene. "
            "The start list is not exhaustive: include NPCs added or moved through presence_updates, "
            "including an NPC who enters and leaves within the same turn."
        )
        lens["review_required_every_turn"] = True
        lens["review_numeric_results"] = [
            "updated",
            "unchanged",
            "no_numeric_dimension_justified",
        ]
        lens["new_dimension_initialization_scale"] = {
            "1-20": "weak but durable",
            "21-40": "noticeable",
            "41-60": "pronounced",
            "61-80": "strong",
            "81-100": "dominant or extreme",
        }
        for row in lens.get("relations_in_current_scene", []) if isinstance(lens.get("relations_in_current_scene"), list) else []:
            if not isinstance(row, dict):
                continue
            owner_id = str(row.get("owner_character_id") or "")
            if owner_id in physical_set:
                row["participation_mode"] = "physical"
            elif owner_id in remote_set:
                row["participation_mode"] = "remote"
        context["relationship_lens"] = lens

    # Persistence/chronology instructions live once in runtime_rules.
    # Do not mirror the same directing prose into a second packet contract.
    context.pop("persistence_contract", None)

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
    session_migrations.ensure_current_session_data(session_id, invalidate_pending=True)
    _current_pointer_guard(session_id)
    _strip_legacy_pov_rule_from_session_source(root)
    game_day._sync_session_game_day(session_id)
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
            (root / "turn_packet.json").unlink(missing_ok=True)
            pending = {}

    # Build once in memory, then serialize only the final writer packet.
    # This avoids storage -> packet -> read -> rewrite -> packet round-trips.
    context = session_runtime.build_turn_context(session_id, user_input)
    packet = {
        "packet_id": secrets.token_urlsafe(12),
        "prepared_for_turn": expected_turn,
        "user_input": user_input,
        "relevant_character_ids": [
            str(value) for value in context.get("relevant_character_ids", []) if value
        ],
        "chunk_count": 0,
        "read_chunks": [],
        "chunks": [],
        "runtime_revision": runtime_access.runtime_revision(),
        "data_schema_version": session_migrations.CURRENT_DATA_SCHEMA_VERSION,
    }
    base = {
        "packet_id": packet["packet_id"],
        "prepared_for_turn": expected_turn,
        "chunk_count": 0,
        "relevant_character_ids": packet["relevant_character_ids"],
    }
    return _prepare_context(
        session_id,
        base,
        packet_override=packet,
        context_override=context,
    )


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


def _relationship_scene_participants(
    state_before: Dict[str, Any],
    state_after: Dict[str, Any],
    extracted: Dict[str, Any],
    *,
    cards: List[Dict[str, Any]],
    user_input: str,
    scene_output: str = "",
) -> List[str]:
    """Return every NPC who actually participated at any point in this turn."""
    result: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        resolved = session_runtime._resolve_character_id(cards, value)
        cid = str(resolved or value or "").strip()
        if cid and cid not in result:
            result.append(cid)

    for value in relationship_file_runtime.physical_participant_ids(
        state_before,
        state_after,
        extracted,
        cards=cards,
    ):
        add(value)

    # Remote participation is broader than physical review scope and is layered
    # on top of the canonical physical participant helper.
    for value in storage._remote_character_ids(state_before):
        add(value)
    for value in storage._remote_character_ids(state_after):
        add(value)

    # A remote exchange can also begin and end inside one turn. The remote
    # communication memory is created before relationship persistence.
    for row in extracted.get("dialogue_memory_add", []) if isinstance(extracted.get("dialogue_memory_add"), list) else []:
        if not isinstance(row, dict) or str(row.get("mode") or "").casefold() != "remote":
            continue
        participants = row.get("participants") or row.get("participant_ids") or []
        if isinstance(participants, str):
            participants = [participants]
        for value in participants if isinstance(participants, list) else []:
            add(value)

    # Direct user-input messages are persisted as private communication even when
    # no remote state remains open at the end of the scene.
    for row in private_knowledge_runtime.extract_private_communications(str(user_input or ""), cards):
        if isinstance(row, dict):
            add(row.get("recipient_id"))

    for match in re.finditer(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*(?:\([^\n)]{1,80}\))?\s*[—-]\s*", str(scene_output or "")):
        add(match.group("speaker"))

    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    return [cid for cid in result if cid and cid != pov_id]


def _apply_relationship_changes(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    updates = extracted.get("relationship_updates") if isinstance(extracted.get("relationship_updates"), list) else []
    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    state_after = storage._deep_merge(state, patch)
    pov = state_after.get("pov") if isinstance(state_after.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    participants = _relationship_scene_participants(
        state,
        state_after,
        extracted,
        cards=cards,
        user_input=str(result.get("user_input") or ""),
        scene_output=str(result.get("scene_output") or ""),
    )
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1

    store = result.get("_relationships_after")
    if not isinstance(store, dict):
        store = relationship_file_runtime.load(root, cards=cards, state=state, pov_id=pov_id)

    store = relationship_file_runtime.ensure_participant_records(store, participants, cards)
    try:
        store = relationship_file_runtime.apply_updates(
            store,
            updates,
            cards=cards,
            pov_id=pov_id,
            turn_number=turn_number,
            participant_ids=participants,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": str(exc), "message": "Invalid NPC-to-POV relationship update."},
        ) from exc

    source_ids = {
        storage._card_id(card)
        for card in storage._normalise_cards(source.get("characters", []))
        if storage._card_id(card)
    }
    registry = (
        state.get("world", {}).get("cast_registry", {})
        if isinstance(state.get("world"), dict)
        else {}
    )
    strict = extracted.get("runtime_rules_reviewed") is True
    if strict:
        for character_id in participants:
            cid = str(character_id or "")
            if not cid or cid == pov_id or cid in source_ids:
                continue
            card = next(
                (card for card in cards if storage._card_id(card) == cid),
                None,
            )
            registry_row = registry.get(cid) if isinstance(registry, dict) else None
            story_created = bool(card) and (
                not source_ids
                or cid not in source_ids
            )
            if isinstance(registry_row, dict):
                story_created = str(registry_row.get("origin") or "") == "story_created"
            if not story_created:
                continue
            relation = relationship_file_runtime.character_relation(store, cid) or {}
            dimensions = relation.get("dimensions") if isinstance(relation.get("dimensions"), dict) else {}
            if not dimensions:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "RELATIONSHIP_INITIALIZATION_REQUIRED",
                        "message": "A persistent story NPC who participated with POV needs at least one real NPC-to-POV relationship dimension.",
                    },
                )

    result["_relationships_after"] = store
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
    updates = extracted.get("npc_relationship_updates") if isinstance(extracted.get("npc_relationship_updates"), list) else []
    if not updates:
        return result

    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    store = result.get("_relationships_after")
    if not isinstance(store, dict):
        store = relationship_file_runtime.load(root, cards=cards, state=state, pov_id=pov_id)

    try:
        store = relationship_file_runtime.apply_npc_updates(
            store,
            updates,
            cards=cards,
            pov_id=pov_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": str(exc), "message": "Invalid NPC-to-NPC relationship update."},
        ) from exc

    result["_relationships_after"] = store
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
    state_patch = deepcopy(extracted.get("state_patch")) if isinstance(extracted.get("state_patch"), dict) else {}
    for key in ("relationships", "relationship_documents", "relationship_schemas", "npc_relationships"):
        state_patch.pop(key, None)
    extracted["state_patch"] = state_patch

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


def _strip_relationship_review(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove validated review evidence before durable turn persistence."""
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if isinstance(extracted, dict):
        extracted.pop("relationship_review", None)
    return result


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_technical_state_patch(payload)
    prepared = _prepare_profile_persistence(session_id, payload)
    prepared = private_knowledge_runtime.add_direct_communication_memory(session_id, prepared)
    prepared = scene_presence_runtime._apply_presence_contract(
        prepared,
        root=storage.SESSIONS_DIR / session_id,
    )
    prepared = private_knowledge_runtime.normalize_dialogue_memory_modes(session_id, prepared)
    prepared = private_knowledge_runtime.add_scene_remote_communication_memory(session_id, prepared)
    # Preserve the existing private-communication firewall and its error codes,
    # then apply the broader scene provenance guard. Everything above is an
    # in-memory payload transformation; no state has been persisted yet.
    private_knowledge_runtime.validate_private_knowledge(session_id, prepared)
    scene_knowledge_guard.validate_scene_output(session_id, prepared)
    prepared = _apply_story_and_intent_updates(session_id, prepared)
    prepared = _apply_npc_relationship_updates(session_id, prepared)
    prepared = _apply_relationship_changes(session_id, prepared)
    relationship_review_runtime.validate_relationship_review(session_id, prepared)
    scene_progression_runtime.validate_scene_progression(session_id, prepared)
    prepared = _strip_relationship_review(prepared)
    prepared = scene_progression_runtime.strip_progression_proof(prepared)
    prepared = cast_registry_runtime._with_registry_patch(session_id, prepared)
    prepared = _normalise_chronology_for_save(session_id, prepared)
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
    saved["saved_chronology_events"] = len(
        prepared.get("extracted", {}).get("chronology", [])
        if isinstance(prepared.get("extracted"), dict)
        else []
    )
    if saved.get("audit_due") is True:
        saved["required_audit"] = fast_audit_runtime.get_audit_snapshot(session_id)
    saved["turn_pipeline_version"] = PIPELINE_VERSION
    return saved


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    prepared = memory_integrity_runtime._canonicalize_memory_payload(
        session_id,
        payload,
        audit=True,
    )
    return dict(stability_runtime._atomic_commit_audit(session_id, prepared))


def continue_session(session_id: str) -> Dict[str, Any]:
    stability_runtime._recover_session(session_id)
    migration = session_migrations.ensure_current_session_data(session_id, invalidate_pending=True)
    result = dict(_BASE_CONTINUE(session_id))
    result["data_schema_version"] = migration["data_schema_version"]
    result["runtime_revision"] = runtime_access.runtime_revision()
    for key in ("relationships", "relationship_documents", "relationship_schemas", "npc_relationships"):
        result.pop(key, None)
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

    meta = storage._read_json(root / "meta.json", {})
    if meta.get("audit_required"):
        result["audit_required"] = True
        result["required_audit"] = fast_audit_runtime.get_audit_snapshot(session_id)
        result["instruction"] = "Complete required_audit before preparing the next gameplay turn."
        return result

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
    # Durable chronology selection is data retrieval, not directing.
    chronology_integrity_runtime._ORIGINAL_SELECT = session_runtime._select_chronology_context
    session_runtime._select_chronology_context = chronology_integrity_runtime._select_chronology_context

    # Safe public storage helpers.
    storage.get_turn_packet_chunk = runtime_fixes.get_turn_packet_chunk
    storage.save_novel = runtime_fixes.save_novel
    storage.get_novel = runtime_fixes.get_novel

    # Required audits reuse the existing public chunk/commit Actions; hidden audit endpoints remain internal compatibility routes.
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
