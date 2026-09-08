from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from . import storage
from .relationship_runtime import build_relationship_lens
from .runtime_access import runtime_documents


RECENT_MEMORY_TURNS = 30
MAX_FULL_MEMORY_RECORDS_PER_TYPE = 36
MAX_HISTORICAL_KNOWLEDGE_SUMMARY = 220


def _normalise_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _resolve_character_id(cards: List[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    needle = _normalise_name(value)
    if not needle:
        return None
    for card in cards:
        cid = storage._card_id(card)
        if _normalise_name(cid) == needle:
            return cid
        if any(_normalise_name(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _card_name(card: Dict[str, Any], fallback: str) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("name") or card.get("full_name") or identity.get("name") or fallback)


def _present_npc_candidates(cards: List[Dict[str, Any]], state: Dict[str, Any], lens: Dict[str, Any]) -> List[Dict[str, Any]]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    by_id = {storage._card_id(card): card for card in cards if storage._card_id(card)}
    saved = {
        str(item.get("owner_character_id")): item
        for item in lens.get("relations_in_current_scene", [])
        if isinstance(item, dict) and item.get("owner_character_id")
    }
    result: List[Dict[str, Any]] = []
    for character_id in storage._present_character_ids(state):
        character_id = str(character_id)
        if not character_id or character_id == pov_id:
            continue
        card = by_id.get(character_id, {})
        relation = saved.get(character_id)
        dimensions = relation.get("dimensions", []) if isinstance(relation, dict) else []
        result.append(
            {
                "character_id": character_id,
                "name": _card_name(card, character_id),
                "has_saved_relationship": bool(dimensions),
                "saved_dimensions": deepcopy(dimensions),
                "initialization_rule": (
                    "If saved_dimensions exist, continue exactly this relationship. If they are empty, this NPC still must be evaluated during the scene. "
                    "As soon as the NPC meaningfully perceives or interacts with POV and a real attitude exists, initialize 1-3 natural relationship dimensions "
                    "from character, goals, knowledge and current interaction and show them in the footer. Do not leave the relationship block empty merely because this is a new chat, session or baseline."
                ),
            }
        )
    return result


def _session_memory(context: Dict[str, Any]) -> Dict[str, Any]:
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    session_id = session.get("session_id")
    if not session_id:
        return {"characters": {}}
    root = storage.SESSIONS_DIR / str(session_id)
    return storage._normalise_memory(storage._read_json(root / "memory.json", {}))


def _explicit_input_character_ids(cards: List[Dict[str, Any]], user_input: Any) -> List[str]:
    text = str(user_input or "").casefold().replace("ё", "е")
    if not text:
        return []
    selected: List[str] = []
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        aliases = [cid, *storage._card_names(card)]
        for alias in aliases:
            needle = str(alias or "").strip().casefold().replace("ё", "е")
            if len(needle) < 2:
                continue
            if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text):
                selected.append(cid)
                break
    return list(dict.fromkeys(selected))


def _scene_character_ids(context: Dict[str, Any], state: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[str]:
    values: List[str] = []
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        values.append(str(pov["character_id"]))
    values.extend(str(value) for value in storage._present_character_ids(state) if value)
    values.extend(_explicit_input_character_ids(cards, context.get("user_input")))
    valid = {storage._card_id(card) for card in cards}
    return [value for value in dict.fromkeys(values) if value in valid]


def _selected_cards(cards: List[Dict[str, Any]], character_ids: List[str]) -> List[Dict[str, Any]]:
    wanted = set(character_ids)
    return [
        {"character_id": storage._card_id(card), "card": deepcopy(card)}
        for card in cards
        if storage._card_id(card) in wanted
    ]


def _record_turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        return int(item.get("learned_turn") or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _is_durable_memory(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    importance = str(item.get("importance") or "").casefold()
    return bool(
        importance in {"anchor", "critical", "major"}
        or item.get("anchor") is True
        or item.get("durable") is True
        or item.get("pinned") is True
        or item.get("permanent") is True
    )


def _memory_summary(item: Dict[str, Any]) -> str:
    for key in (
        "fact", "summary", "event", "description", "text", "content", "topic", "memory", "note", "detail",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:MAX_HISTORICAL_KNOWLEDGE_SUMMARY]
    raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return raw[:MAX_HISTORICAL_KNOWLEDGE_SUMMARY]


def _record_id(item: Dict[str, Any]) -> str | None:
    for key in ("fact_id", "event_id", "topic_id", "id"):
        if item.get(key):
            return str(item[key])
    return None


def _working_records(records: Any, current_turn: int) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    values = [deepcopy(item) for item in records if isinstance(item, dict)] if isinstance(records, list) else []
    threshold = max(0, current_turn - RECENT_MEMORY_TURNS + 1)
    full_candidates = [
        item for item in values
        if _is_durable_memory(item) or _record_turn(item) >= threshold or _record_turn(item) == 0
    ]
    full_candidates.sort(key=lambda item: (_is_durable_memory(item), _record_turn(item)), reverse=True)
    full = full_candidates[:MAX_FULL_MEMORY_RECORDS_PER_TYPE]
    full_ids = {_record_id(item) or json.dumps(item, ensure_ascii=False, sort_keys=True) for item in full}
    omitted = [
        item for item in values
        if (_record_id(item) or json.dumps(item, ensure_ascii=False, sort_keys=True)) not in full_ids
    ]
    return full, omitted


def _working_memory_bucket(bucket: Any, current_turn: int) -> Dict[str, Any]:
    source = bucket if isinstance(bucket, dict) else {}
    knowledge, old_knowledge = _working_records(source.get("knowledge", []), current_turn)
    experiences, old_experiences = _working_records(source.get("experiences", []), current_turn)
    dialogue, old_dialogue = _working_records(source.get("dialogue_memory", []), current_turn)
    historical_knowledge_catalog = []
    for item in old_knowledge:
        row = {
            "fact_id": _record_id(item),
            "learned_turn": _record_turn(item),
            "summary": _memory_summary(item),
        }
        if item.get("confidence") is not None:
            row["confidence"] = item.get("confidence")
        historical_knowledge_catalog.append({key: value for key, value in row.items() if value not in (None, "", 0)})
    return {
        "knowledge": knowledge,
        "experiences": experiences,
        "dialogue_memory": dialogue,
        "historical_knowledge_catalog": historical_knowledge_catalog,
        "older_history_available": {
            "knowledge_records_not_full": len(old_knowledge),
            "experience_records_not_full": len(old_experiences),
            "dialogue_records_not_full": len(old_dialogue),
            "retrieval": "prepareCharacterBundleRead -> getCharacterBundleChunk",
        },
    }


def _selected_memory(memory: Dict[str, Any], character_ids: List[str], current_turn: int) -> Dict[str, Any]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    return {
        character_id: _working_memory_bucket(buckets.get(character_id, {}), current_turn)
        for character_id in character_ids
    }


def _compact_scene_state(state: Dict[str, Any], scene_ids: List[str]) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    result.pop("relationships", None)
    result.pop("relationship_documents", None)
    result.pop("relationship_schemas", None)
    result.pop("threads", None)
    runtime = result.get("characters")
    if isinstance(runtime, dict):
        wanted = set(scene_ids)
        result["characters"] = {
            str(character_id): deepcopy(info)
            for character_id, info in runtime.items()
            if str(character_id) in wanted
        }
    return result


def _prune_scene_character_lenses(context: Dict[str, Any], scene_ids: List[str]) -> None:
    lenses = context.get("scene_characters")
    if not isinstance(lenses, dict):
        return
    wanted = set(scene_ids)
    context["scene_characters"] = {
        str(character_id): deepcopy(lens)
        for character_id, lens in lenses.items()
        if str(character_id) in wanted and isinstance(lens, dict)
    }
    for character_id, lens in context["scene_characters"].items():
        lens.pop("personal_memory", None)
        lens["personal_memory_path"] = f"character_memory[{character_id}]"


def _strip_cast_relationship_payload(context: Dict[str, Any]) -> None:
    cast_index = context.get("cast_index")
    if isinstance(cast_index, list):
        for row in cast_index:
            if isinstance(row, dict):
                row.pop("relationship_to_pov", None)


def inject_required_turn_context(context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Inject a scene-scoped working set while keeping complete persistent data in Railway."""
    memory = _session_memory(context)
    documents = runtime_documents()
    scene_ids = _scene_character_ids(context, state, cards)
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    current_turn = int(session.get("turn_number", 0) or 0)
    scene_cards = _selected_cards(cards, scene_ids)
    scene_memory = _selected_memory(memory, scene_ids, current_turn)

    context["relevant_character_ids"] = scene_ids
    context["scene_state"] = _compact_scene_state(state, scene_ids)
    _prune_scene_character_lenses(context, scene_ids)
    _strip_cast_relationship_payload(context)

    context.pop("runtime_documents", None)
    context["runtime_rules"] = documents["rules"]
    context["scene_builder"] = documents["scene_builder"]
    context["runtime_document_paths"] = {
        "rules": "runtime_rules",
        "scene_builder": "scene_builder",
    }
    context["scene_builder_instruction"] = (
        "MANDATORY. Read scene_builder completely before writing and follow its FORMAT exactly. "
        "Do not shorten, reorder, omit or replace its blocks."
    )
    context["pov_participation_instruction"] = (
        "MANDATORY GLOBAL POV RULE. POV must remain an active participant throughout the scene. "
        "Write ordinary in-character POV dialogue, reactions, thoughts and small actions without asking permission. "
        "Do not reduce POV to silence, one-word replies or body-only reactions merely to preserve player agency. "
        "Stop only before genuinely consequential POV choices defined by the contract."
    )
    context["npc_agency_instruction"] = (
        "MANDATORY GLOBAL NPC AGENCY RULE. NPC behavior comes from that NPC's character, desires, goals, advantage, fears, relationships, knowledge, duties and current situation. "
        "Do not replace character logic with universal therapy, etiquette or author-approved psychological correctness. "
        "If the specific NPC would act, let them act without forcing a preliminary permission question. "
        "Do not praise restraint or narrate omitted action merely to model healthy behavior. Consequential POV reactions and choices remain with the player."
    )

    relationship_lens = build_relationship_lens(
        state,
        cards=cards,
        present_character_ids=storage._present_character_ids,
        resolve_character_id=_resolve_character_id,
    )
    relationship_lens["present_npc_candidates"] = _present_npc_candidates(cards, state, relationship_lens)
    relationship_lens["initialization_required"] = True
    relationship_lens["initialization_instruction"] = (
        "A missing saved relation is NOT a reason to omit relationships forever. Evaluate every present NPC candidate. "
        "For an NPC with saved dimensions, preserve them. For an NPC without saved dimensions, once this scene establishes a real directional attitude toward POV, "
        "create 1-3 specific dimensions natural to that NPC and print them in the visible footer. Do not use a generic placeholder. "
        "The first appearance may omit /delta because there is no prior numeric baseline."
    )
    context["relationship_lens"] = relationship_lens
    context["relationship_lens_instruction"] = (
        "MANDATORY. relationship_lens is the causal relationship layer and is authoritative for current NPC->POV relations. "
        "Every physically present NPC is listed in present_npc_candidates. Existing dimensions MUST appear in the visible Relationships footer. "
        "Carry saved dimensions across absences and later meetings. Full relationship stores remain persistent in Railway and are intentionally omitted from scene_state transport."
    )

    context["character_cards"] = scene_cards
    context["character_memory"] = scene_memory
    context["character_context_instruction"] = (
        "Full character_cards are transported only for POV, characters physically present at turn start, and registered characters explicitly named in the current player input. "
        "This includes an offscreen person the player is explicitly messaging, calling or otherwise addressing. The registry still contains every registered character. "
        "If any other offscreen character is about to speak, send/receive a message, call, enter, or materially act, load that character's full bundle with prepareCharacterBundleRead and every getCharacterBundleChunk BEFORE writing that character. "
        "character_memory is a bounded working copy: recent/durable records plus a compact catalog of older learned facts. Complete lifetime memory remains persistent and is available through the same character bundle read."
    )
    context["knowledge_guard"] = {
        "mandatory": True,
        "personal_memory_path": "character_memory[character_id]",
        "present_at_turn_start_path": "present_character_ids_at_turn_start",
        "author_only_paths": [
            "character_cards", "character_registry", "cast_index", "scene_state", "relationships", "active_threads",
            "novel", "novel_rules", "novel_lore", "hidden_lore", "world_canon", "story_direction",
            "chronology_recent", "recent_turns", "character_memory[OTHER_CHARACTER_ID]",
        ],
        "instruction": (
            "MANDATORY KNOWLEDGE FIREWALL. Before every NPC line, message, call, inference, recognition or deliberate action, identify that NPC and verify the exact source for every referenced fact. "
            "Past knowledge may come only from that NPC's own character_memory, including historical_knowledge_catalog. If an exact older detail is needed beyond the working copy, load that NPC's full character bundle first. "
            "A fact created during the current turn may be used only after that NPC personally perceived it or received it through an explicit communication channel established in the scene. "
            "Author context, recent turns, chronology, cards, registry, scene state, another character's memory, relationship values and narrative plausibility are never character knowledge sources. "
            "Private POV thoughts, phone screens, typed or received messages, calls not heard by the NPC, letters, photos, headphones and other private content remain unknown without established access. "
            "An NPC outside the physical scene does not know what is happening there merely because the author knows it. Before an offscreen NPC sends a message, calls or reacts to a current event, verify how that NPC learned the specific event first. "
            "Arrival after an event and departure before an event do not grant retroactive knowledge. Inference may use only premises already available to that NPC and may not reproduce an unavailable exact detail. "
            "If any drafted line or action uses a fact without a valid source, rewrite or delete it before output. The leak is not canon and must not be persisted."
        ),
        "offscreen_contact_rule": (
            "Remote communication counts as character participation. An offscreen sender/recipient needs a loaded full card before their authored dialogue or deliberate reaction, and may refer only to facts actually available to that character."
        ),
    }
    context["working_context_contract"] = {
        "persistent_storage_is_complete": True,
        "turn_packet_is_scene_scoped": True,
        "no_persistent_data_deleted": True,
        "full_cards_only_for_active_participants": True,
        "lifetime_memory_stays_persistent": True,
        "scene_state_relationship_stores_omitted": True,
        "single_runtime_document_copy": True,
        "stable_scene_builder_paths": True,
        "instruction": (
            "Railway stores complete source, live cards, personal memories, relationships and chronology. The turn packet is intentionally a bounded working set. "
            "Omitted dormant dossiers, older full memory records and relationship documents were not deleted; retrieve a character bundle on demand before an offscreen character participates."
        ),
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = scene_cards
    author_context["knowledge_quarantine"] = (
        "Everything in author_context is objective author/engine truth only. Never use it as a character knowledge source. "
        "A character may use a fact only from that character's own personal memory or from a perception/communication channel that actually reached that character."
    )
    context["author_context"] = author_context
    return context
