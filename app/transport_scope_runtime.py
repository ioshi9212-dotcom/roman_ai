from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


_MAX_THREAD_TEXT = 1200
_MAX_MEMORY_FIELD_TEXT = 700


def _bounded_transport_value(value: Any, *, max_text: int = _MAX_THREAD_TEXT) -> Any:
    if isinstance(value, str):
        if len(value) <= max_text:
            return value
        return value[:max_text] + "…[full value remains in persistent storage]"
    if isinstance(value, dict):
        return {str(key): _bounded_transport_value(item, max_text=max_text) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_transport_value(item, max_text=max_text) for item in value]
    return deepcopy(value)


def _compact_starting_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = deepcopy(value)
    for key in ("relationships", "relationship_documents", "relationship_schemas", "threads", "characters"):
        result.pop(key, None)
    return _bounded_transport_value(result)


def _compact_character_memory(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, Any] = {}
    for character_id, bucket in value.items():
        if not isinstance(bucket, dict):
            result[str(character_id)] = _bounded_transport_value(bucket, max_text=_MAX_MEMORY_FIELD_TEXT)
            continue
        row: Dict[str, Any] = {}
        for key, item in bucket.items():
            if key == "knowledge":
                # Knowledge is factual authority and must not be text-truncated in
                # transport. Packet chunking already provides the size boundary.
                row[key] = deepcopy(item)
            else:
                row[key] = _bounded_transport_value(item, max_text=_MAX_MEMORY_FIELD_TEXT)
        result[str(character_id)] = row
    return result


def _relationship_snapshot_from_persistent_state(state: Dict[str, Any]) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    elif isinstance(present, str):
        present = [present]
    elif not isinstance(present, list):
        present = []
    ids = []
    for value in present:
        if isinstance(value, dict):
            value = value.get("character_id") or value.get("id") or value.get("name")
        if value:
            ids.append(str(value))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    result: Dict[str, Any] = {}
    for owner_id in dict.fromkeys(ids):
        if not owner_id or owner_id == pov_id:
            continue
        relation = relationships.get(owner_id)
        metrics = {
            str(key): number
            for key, number in relation.items()
            if isinstance(relation, dict) and isinstance(number, (int, float)) and not isinstance(number, bool)
        } if isinstance(relation, dict) else {}
        result[owner_id] = {"metrics": metrics, "has_saved_baseline": bool(metrics)}
    return result


def _strip_legacy_full_payloads(context: Dict[str, Any], *, persistent_state: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(context)

    for key in (
        "state",
        "memory",
        "chronology",
        "characters",
        "all_character_cards",
        "memory_full",
        "source_full",
        "state_full",
        "scene_character_cards",
        "scene_character_memory",
        "character_registry_index",
    ):
        result.pop(key, None)

    source = result.get("source")
    if isinstance(source, dict):
        source = deepcopy(source)
        source.pop("characters", None)
        source.pop("locations", None)
        source.pop("canon_notes", None)
        result["source"] = source

    novel_source = result.get("novel_source")
    if isinstance(novel_source, dict):
        novel_source = deepcopy(novel_source)
        novel_source.pop("characters", None)
        novel_source.pop("locations", None)
        novel_source.pop("canon_notes", None)
        result["novel_source"] = novel_source

    author = result.get("author_context")
    if isinstance(author, dict):
        author = deepcopy(author)
        for key in (
            "state",
            "memory",
            "chronology",
            "characters",
            "all_character_cards",
            "memory_full",
            "source_full",
            "state_full",
            "character_cards",
            "chronology_recent",
            "recent_turns",
        ):
            author.pop(key, None)
        result["author_context"] = author

    result["starting_state"] = _compact_starting_state(result.get("starting_state"))
    result["character_memory"] = _compact_character_memory(result.get("character_memory"))
    if "active_threads" in result:
        result["active_threads"] = _bounded_transport_value(result.get("active_threads"))

    policy = result.get("relationship_policy") if isinstance(result.get("relationship_policy"), dict) else {}
    policy["authoritative_start_snapshot"] = _relationship_snapshot_from_persistent_state(persistent_state)
    policy["authoritative_start_snapshot_note"] = "Стартовые значения взяты из persistent state."
    result["relationship_policy"] = policy

    result["character_context_instruction"] = "Knowledge активных персонажей передаётся полностью; прочая память bounded. Relevant offscreen NPC можно загрузить по state/intents/threads до участия."
    contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "turn_packet_is_scene_scoped": True,
            "legacy_full_state_memory_chronology_in_packet": False,
            "dormant_full_dossiers_in_packet": False,
            "lifetime_memory_in_packet": False,
            "active_character_knowledge_complete": True,
            "knowledge_text_not_truncated_in_transport": True,
            "oversized_memory_text_bounded_in_transport": True,
            "full_relationship_documents_in_packet": False,
            "full_starting_state_in_packet": False,
            "active_threads_text_is_bounded": True,
            "dormant_character_retrieval": "chunked_when_relevant",
            "remote_communication_requires_loaded_dossier": True,
            "persistent_storage_is_complete": True,
            "same_pending_turn_prepare_is_idempotent": True,
        }
    )
    result["working_context_contract"] = contract
    return result
