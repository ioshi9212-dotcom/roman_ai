from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List, Tuple


_METADATA_KEYS = (
    "opinion",
    "current_dynamic",
    "beliefs_about_target",
    "unresolved_between_them",
    "relationship_type",
    "relationship_context",
)


def relationship_participant_ids(
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    extracted: Dict[str, Any],
    *,
    resolve_character_id: Callable[[List[Dict[str, Any]], Any], str | None],
    present_character_ids: Callable[[Dict[str, Any]], List[str]],
    state_after: Dict[str, Any] | None = None,
) -> set[str]:
    """Return ids with concrete evidence of participation in this turn.

    Mention in user input, cast pressure, active threads, or packet relevance is not participation.
    Physical presence/transition and explicit current-turn memory ownership are.
    """
    result = {str(value) for value in present_character_ids(state_before) if value}
    if isinstance(state_after, dict):
        result.update(str(value) for value in present_character_ids(state_after) if value)

    def add(raw: Any) -> None:
        resolved = resolve_character_id(cards, raw)
        if resolved:
            result.add(str(resolved))

    for row in extracted.get("presence_updates", []) if isinstance(extracted.get("presence_updates"), list) else []:
        if isinstance(row, dict):
            add(row.get("character_id") or row.get("id") or row.get("name"))

    state_patch = extracted.get("state_patch")
    current_patch = state_patch.get("current") if isinstance(state_patch, dict) and isinstance(state_patch.get("current"), dict) else {}
    direct = current_patch.get("present_characters")
    if isinstance(direct, list):
        for value in direct:
            add(value)
    elif direct not in (None, "", {}, []):
        add(direct)

    for row in extracted.get("character_upserts", []) if isinstance(extracted.get("character_upserts"), list) else []:
        if isinstance(row, dict):
            add(row.get("character_id") or row.get("id") or row.get("name"))

    dialogue_keys = (
        "participants", "participant_ids", "character_id", "asked_by", "asked_to",
        "speaker", "listener", "said_by", "heard_by",
    )
    for row in extracted.get("dialogue_memory_add", []) if isinstance(extracted.get("dialogue_memory_add"), list) else []:
        if not isinstance(row, dict):
            continue
        for key in dialogue_keys:
            value = row.get(key)
            if isinstance(value, list):
                for item in value:
                    add(item)
            elif value not in (None, ""):
                add(value)

    for field in ("knowledge_add", "experiences_add"):
        for row in extracted.get(field, []) if isinstance(extracted.get(field), list) else []:
            if isinstance(row, dict):
                add(row.get("character_id") or row.get("owner_character_id"))

    return result


def apply_relationship_metadata(
    state: Dict[str, Any],
    rows: Iterable[Dict[str, Any]] | None,
    *,
    turn_number: int,
) -> Tuple[Dict[str, Any], bool]:
    result = deepcopy(state if isinstance(state, dict) else {})
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return result, False

    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    if not pov_id:
        return result, False

    docs = result.get("relationship_documents")
    docs = deepcopy(docs) if isinstance(docs, dict) else {}
    changed = False

    for raw in rows:
        owner_id = str(raw.get("character_id") or "")
        if not owner_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relations = doc.get("relations")
        if not isinstance(relations, list):
            relations = []
            doc["relations"] = relations
        relation = next(
            (
                row for row in relations
                if isinstance(row, dict)
                and str(row.get("target_character_id") or "") == pov_id
            ),
            None,
        )
        if relation is None:
            relation = {
                "target_character_id": pov_id,
                "relationship_type": "установленная связь",
                "relationship_context": "",
                "current_dynamic": "",
                "dimensions": [],
                "beliefs_about_target": [],
                "unresolved_between_them": [],
                "dynamic_constraints": [],
                "change_reasons": [],
                "last_changed_turn": 0,
            }
            relations.append(relation)

        before = deepcopy(relation)
        if "opinion" in raw:
            relation["current_dynamic"] = str(raw.get("opinion") or "").strip()
        if "current_dynamic" in raw:
            relation["current_dynamic"] = str(raw.get("current_dynamic") or "").strip()
        for key in ("beliefs_about_target", "unresolved_between_them"):
            if key in raw and isinstance(raw[key], list):
                relation[key] = deepcopy(raw[key])
        for key in ("relationship_type", "relationship_context"):
            if key in raw:
                relation[key] = str(raw.get(key) or "").strip()
        if relation != before:
            relation["last_changed_turn"] = int(turn_number)
            changed = True

    if changed:
        result["relationship_documents"] = docs
    return result, changed


def metadata_rows_from_payload(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    rows = payload.get("_relationship_metadata")
    return [deepcopy(row) for row in rows] if isinstance(rows, list) else []
