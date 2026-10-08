from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from . import personal_memory_transport, relationship_file_runtime, storage
from .profile_templates import render_knowledge_journal
from .runtime_access import runtime_documents
from .scene_compaction_runtime import active_memory_records, transport_knowledge_records, transport_knowledge_journal


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
    for character_id in storage._scene_participant_ids(state):
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
    values.extend(str(value) for value in storage._scene_participant_ids(state) if value)
    # A name mention alone must not pull an offscreen dossier into the working set.
    # Offscreen participation is loaded explicitly through prepareCharacterBundleRead.
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


def _recency_turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        if item.get("last_learned_turn") not in (None, ""):
            return int(item["last_learned_turn"])
    except (TypeError, ValueError):
        pass
    return _record_turn(item)


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
    values = active_memory_records(records)
    threshold = max(0, current_turn - RECENT_MEMORY_TURNS + 1)
    full_candidates = [
        item for item in values
        if _is_durable_memory(item) or _recency_turn(item) >= threshold or _recency_turn(item) == 0
    ]
    full_candidates.sort(key=lambda item: (_is_durable_memory(item), _recency_turn(item)), reverse=True)
    full = full_candidates[:MAX_FULL_MEMORY_RECORDS_PER_TYPE]
    full_ids = {_record_id(item) or json.dumps(item, ensure_ascii=False, sort_keys=True) for item in full}
    omitted = [
        item for item in values
        if (_record_id(item) or json.dumps(item, ensure_ascii=False, sort_keys=True)) not in full_ids
    ]
    return full, omitted


def _working_memory_bucket(
    bucket: Any,
    current_turn: int,
    *,
    character_id: str = "",
    cards: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    source = bucket if isinstance(bucket, dict) else {}

    # Knowledge is factual authority for dialogue. Every active knowledge record for
    # a scene participant must therefore reach the model; a recency cap can make a
    # character "forget" an older fact even though it is still persisted.
    knowledge = transport_knowledge_records(source.get("knowledge", []))
    journal_rows = (
        source.get("knowledge_journal", [])
        if isinstance(source.get("knowledge_journal"), list)
        else []
    )
    knowledge_journal = render_knowledge_journal(transport_knowledge_journal(journal_rows))

    # Experiences and dialogue recollection are supporting context rather than the
    # factual knowledge authority, so they can stay bounded for packet size.
    experiences, old_experiences = _working_records(source.get("experiences", []), current_turn)
    dialogue, old_dialogue = _working_records(source.get("dialogue_memory", []), current_turn)
    if character_id and cards is not None:
        dialogue = personal_memory_transport.personal_dialogue_rows(
            dialogue,
            owner_id=character_id,
            cards=cards,
        )
    return {
        "knowledge": deepcopy(knowledge),
        "knowledge_journal": knowledge_journal,
        "knowledge_journal_entry_count": len(journal_rows),
        "experiences": experiences,
        "dialogue_memory": dialogue,
        "historical_knowledge_catalog": [],
        "knowledge_complete_in_transport": True,
        "older_history_available": {
            "knowledge_records_not_full": 0,
            "experience_records_not_full": len(old_experiences),
            "dialogue_records_not_full": len(old_dialogue),
            "retrieval": "prepareCharacterBundleRead -> getCharacterBundleChunk",
        },
    }


def _selected_memory(
    memory: Dict[str, Any],
    character_ids: List[str],
    current_turn: int,
    cards: List[Dict[str, Any]],
) -> Dict[str, Any]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    return {
        character_id: _working_memory_bucket(
            buckets.get(character_id, {}),
            current_turn,
            character_id=character_id,
            cards=cards,
        )
        for character_id in character_ids
    }


_OFFSCREEN_PHYSICAL_KEYS = {
    "present", "location", "last_location", "zone", "position",
    "clothing", "outfit", "hair", "activity", "inventory",
    "last_seen_turn", "last_seen_game_day",
}


def _compact_scene_state(state: Dict[str, Any], scene_ids: List[str]) -> Dict[str, Any]:
    result = deepcopy(state if isinstance(state, dict) else {})
    result.pop("relationships", None)
    result.pop("relationship_documents", None)
    result.pop("relationship_schemas", None)
    result.pop("npc_relationships", None)
    result.pop("threads", None)
    runtime = result.get("characters")
    if isinstance(runtime, dict):
        wanted = set(scene_ids)
        compact: Dict[str, Any] = {}
        for character_id, raw in runtime.items():
            cid = str(character_id)
            if not isinstance(raw, dict):
                continue
            if cid in wanted:
                compact[cid] = deepcopy(raw)
                continue
            physical = {
                key: deepcopy(raw[key])
                for key in _OFFSCREEN_PHYSICAL_KEYS
                if key in raw and raw[key] not in (None, "", [], {})
            }
            if physical:
                compact[cid] = physical
        result["characters"] = compact
    result["offscreen_physical_state_rule"] = (
        "scene_state.characters may include compact physical continuity for offscreen characters "
        "without loading their dossier. Use location/clothing/items only as state continuity. "
        "Offscreen physical state may itself make participation natural; if so, load that character bundle before material participation."
    )
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
        # NPC→POV relationship state comes only from relationships.json via
        # relationship_lens. Never leave the legacy state hint beside it.
        lens.pop("relationship_to_pov", None)
        lens["personal_memory_path"] = f"character_memory[{character_id}]"


def _strip_cast_relationship_payload(context: Dict[str, Any]) -> None:
    cast_index = context.get("cast_index")
    if isinstance(cast_index, list):
        for row in cast_index:
            if isinstance(row, dict):
                row.pop("relationship_to_pov", None)


def inject_required_turn_context(
    context: Dict[str, Any], cards: List[Dict[str, Any]], state: Dict[str, Any],
    *, memory_override: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Inject scene-scoped data. Reuse the already-read session snapshot when available."""
    memory = memory_override if memory_override is not None else _session_memory(context)
    documents = runtime_documents()
    scene_ids = _scene_character_ids(context, state, cards)
    session = context.get("session") if isinstance(context.get("session"), dict) else {}
    current_turn = int(session.get("turn_number", 0) or 0)
    scene_cards = _selected_cards(cards, scene_ids)
    scene_memory = _selected_memory(memory, scene_ids, current_turn, cards)

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
    context["scene_builder_instruction"] = "scene_builder обязателен; формат не менять."
    context["pov_participation_instruction"] = "POV участвует сам в мелочах и обычной речи; значимые решения оставляй игроку."
    context["npc_agency_instruction"] = "NPC решает из себя: кто он, чего хочет, что чувствует, что знает и во что верит, затем действует. Не прогоняй действие заранее через универсальную правильность, психологию, границы или последствия, если сам NPC об этом не думает."

    session_id = str(session.get("session_id") or "")
    relationship_store = relationship_file_runtime.load(
        storage.SESSIONS_DIR / session_id,
        cards=cards,
        state=state,
        pov_id=str((state.get("pov") or {}).get("character_id") or ""),
    ) if session_id else relationship_file_runtime.build_initial_store(
        cards,
        state,
        str((state.get("pov") or {}).get("character_id") or ""),
    )
    scene_relationships = relationship_file_runtime.scene_snapshot(
        relationship_store,
        storage._scene_participant_ids(state),
    )
    context["relationship_lens"] = {
        "source": relationship_file_runtime.FILE_NAME,
        "direction": "NPC -> POV only",
        "relations_in_current_scene": [
            {
                "owner_character_id": character_id,
                "dynamic": str(row.get("dynamic") or ""),
                "dimensions": [
                    {
                        "label": label,
                        "value": item.get("value"),
                        "last_change": deepcopy(item.get("last_change", {})),
                    }
                    for label, item in row.get("dimensions", {}).items()
                    if isinstance(item, dict)
                ],
            }
            for character_id, row in scene_relationships.items()
            if isinstance(row, dict)
        ],
        "rule": (
            "Единственный канон числовых NPC→POV отношений — relationships.json. "
            "Показатели свободные, максимум 10 активных на NPC; значения 1–100, отрицательных и нулевых показателей в файле нет."
        ),
    }
    context["relationship_lens_instruction"] = "relationship_lens — только scene-view из relationships.json, не отдельное хранилище."

    context["character_cards"] = scene_cards
    context["character_memory"] = scene_memory
    context["character_context_instruction"] = "Участникам сцены knowledge передаётся полностью. Offscreen NPC не требует отдельной причины из state/intents/threads: при инициативе по своему характеру/целям заранее загрузи полный character bundle."
    context["knowledge_guard"] = {
        "mandatory": True,
        "personal_memory_path": "character_memory[character_id]",
        "present_at_turn_start_path": "present_character_ids_at_turn_start",
        "author_only_paths": [
            "character_cards[OTHER_CHARACTER_ID]", "character_registry", "cast_index", "npc_relationship_network", "scene_state", "relationships", "active_threads",
            "novel", "novel_rules", "novel_lore", "hidden_lore", "world_canon", "story_direction", "location_context", "canon_notes_context",
            "chronology_recent", "recent_turns", "character_memory[OTHER_CHARACTER_ID]",
        ],
        "instruction": (
            "NPC использует свою память, self-known факты собственной card и реально полученную информацию. "
            "Если личная деталь нигде не задана, её можно создать через canon_fill и закрепить; чужой/author canon не подмешивать."
        ),
        "offscreen_contact_rule": "Offscreen NPC активен по умолчанию: intent/thread не является условием инициативы. Если он действует или связывается, перед содержательным участием прочитай bundle; это проверка знания, не разрешение.",
    }
    context["working_context_contract"] = {
        "persistent_storage_is_complete": True,
        "turn_packet_is_scene_scoped": True,
        "no_persistent_data_deleted": True,
        "full_cards_only_for_active_participants": True,
        "remote_calls_and_messages_count_as_scene_participation": True,
        "lifetime_memory_stays_persistent": True,
        "active_character_knowledge_complete": True,
        "active_character_knowledge_journal_complete": True,
        "scene_state_relationship_stores_omitted": True,
        "single_runtime_document_copy": True,
        "stable_scene_builder_paths": True,
        "instruction": "Packet bounded; полный канон остаётся в Railway.",
    }

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["character_cards"] = scene_cards
    author_context["knowledge_quarantine"] = "author_context — авторский канон, не личное знание персонажа."
    context["author_context"] = author_context
    return context
