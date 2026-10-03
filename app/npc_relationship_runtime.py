from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, Iterable, List


RELATION_LIST_FIELDS = (
    "beliefs_about_target",
    "unresolved_between_them",
    "dynamic_constraints",
    "interaction_hooks",
)
RELATION_TEXT_FIELDS = (
    "relationship_type",
    "relationship_context",
    "current_dynamic",
    "behavioral_pattern",
    "status",
)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _compact_text(value: Any, limit: int = 500) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    elif isinstance(value, list):
        text = "; ".join(" ".join(str(item).split()) for item in value if item not in (None, ""))
    else:
        text = " ".join(str(value).split())
    return text[:limit] if text else None


def _as_list(value: Any, limit: int = 8) -> List[str]:
    if value in (None, "", [], {}):
        return []
    raw = value if isinstance(value, list) else [value]
    result: List[str] = []
    for item in raw:
        text = _compact_text(item, 300)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _card_id(card: Dict[str, Any]) -> str:
    return str(card.get("character_id") or card.get("id") or "").strip()


def _card_name(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(
        card.get("name")
        or card.get("full_name")
        or identity.get("name")
        or _card_id(card)
    ).strip()


def _aliases(card: Dict[str, Any]) -> List[str]:
    result = [_card_id(card), _card_name(card)]
    surname = card.get("surname")
    if surname:
        result.append(str(surname))
    raw = card.get("aliases")
    if isinstance(raw, list):
        result.extend(str(item) for item in raw if item)
    elif raw:
        result.append(str(raw))
    return [item for item in dict.fromkeys(result) if item]


def _guess_target_from_text(
    owner_id: str,
    text: str,
    cards: Iterable[Dict[str, Any]],
) -> str | None:
    hay = _norm(text)
    if not hay:
        return None
    matches: List[tuple[int, str]] = []
    for card in cards:
        cid = _card_id(card)
        if not cid or cid == owner_id:
            continue
        for alias in _aliases(card):
            needle = _norm(alias)
            if len(needle) < 3:
                continue
            # Russian names are often inflected in free-form setup. A 4-char prefix
            # is enough for the compact director index and avoids requiring a full NLP parser.
            found = needle in hay or (len(needle) >= 4 and needle[:4] in hay)
            if found:
                matches.append((len(needle), cid))
                break
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def _profile_relation_entries(
    owner: Dict[str, Any],
    cards: List[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> List[Dict[str, Any]]:
    owner_id = _card_id(owner)
    raw = owner.get("relationships")
    if raw in (None, "", [], {}):
        return []

    rows: List[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        direct_target = (
            raw.get("target_character_id")
            or raw.get("target_id")
            or raw.get("character_id")
            or raw.get("target")
        )
        if direct_target:
            rows = [raw]
        else:
            rows = []
            for key, value in raw.items():
                if isinstance(value, dict):
                    row = deepcopy(value)
                    row.setdefault("target_character_id", key)
                else:
                    row = {
                        "target_character_id": key,
                        "relationship_context": value,
                    }
                rows.append(row)
    else:
        rows = [raw]

    result: List[Dict[str, Any]] = []
    for raw_row in rows:
        if isinstance(raw_row, dict):
            target_hint = (
                raw_row.get("target_character_id")
                or raw_row.get("target_id")
                or raw_row.get("target")
                or raw_row.get("with")
                or raw_row.get("character_id")
                or raw_row.get("name")
            )
            target_id = resolve_character_id(cards, target_hint) if target_hint else None
            if not target_id:
                target_id = _guess_target_from_text(owner_id, str(raw_row), cards)
            if not target_id or str(target_id) == owner_id:
                continue
            row = {
                "owner_character_id": owner_id,
                "target_character_id": str(target_id),
                "relationship_type": raw_row.get("relationship_type") or raw_row.get("type"),
                "relationship_context": (
                    raw_row.get("relationship_context")
                    or raw_row.get("history")
                    or raw_row.get("context")
                    or raw_row.get("description")
                ),
                "current_dynamic": raw_row.get("current_dynamic") or raw_row.get("dynamic"),
                "behavioral_pattern": raw_row.get("behavioral_pattern") or raw_row.get("behavior"),
                "beliefs_about_target": _as_list(raw_row.get("beliefs_about_target") or raw_row.get("beliefs")),
                "unresolved_between_them": _as_list(raw_row.get("unresolved_between_them") or raw_row.get("unresolved")),
                "dynamic_constraints": _as_list(raw_row.get("dynamic_constraints") or raw_row.get("constraints")),
                "interaction_hooks": _as_list(raw_row.get("interaction_hooks") or raw_row.get("hooks")),
                "status": raw_row.get("status") or "active",
                "source": "profile",
            }
        else:
            text = _compact_text(raw_row, 700)
            if not text:
                continue
            target_id = _guess_target_from_text(owner_id, text, cards)
            if not target_id:
                continue
            row = {
                "owner_character_id": owner_id,
                "target_character_id": str(target_id),
                "relationship_context": text,
                "status": "active",
                "source": "profile",
            }
        result.append({key: value for key, value in row.items() if value not in (None, "", [], {})})
    return result


def _runtime_rows(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = state.get("npc_relationships")
    if not isinstance(store, dict):
        return []
    result: List[Dict[str, Any]] = []
    for owner_id, targets in store.items():
        if not isinstance(targets, dict):
            continue
        for target_id, raw in targets.items():
            if not isinstance(raw, dict):
                continue
            row = deepcopy(raw)
            row["owner_character_id"] = str(owner_id)
            row["target_character_id"] = str(target_id)
            row["source"] = "runtime"
            result.append(row)
    return result


def build_network(
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    *,
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
) -> Dict[str, Any]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    names = {_card_id(card): _card_name(card) for card in cards if _card_id(card)}

    by_pair: Dict[tuple[str, str], Dict[str, Any]] = {}
    for card in cards:
        owner_id = _card_id(card)
        if not owner_id or owner_id == pov_id:
            continue
        for row in _profile_relation_entries(card, cards, resolve_character_id):
            target_id = str(row.get("target_character_id") or "")
            if not target_id or target_id == pov_id or target_id == owner_id:
                continue
            by_pair[(owner_id, target_id)] = row

    for row in _runtime_rows(state):
        owner_id = str(row.get("owner_character_id") or "")
        target_id = str(row.get("target_character_id") or "")
        if not owner_id or not target_id or owner_id == target_id:
            continue
        if owner_id == pov_id or target_id == pov_id:
            continue
        merged = deepcopy(by_pair.get((owner_id, target_id), {}))
        merged.update({key: deepcopy(value) for key, value in row.items() if value not in (None, "")})
        merged["source"] = "runtime"
        by_pair[(owner_id, target_id)] = merged

    relations: List[Dict[str, Any]] = []
    for (owner_id, target_id), raw in by_pair.items():
        row = {
            "owner_character_id": owner_id,
            "owner_name": names.get(owner_id, owner_id),
            "target_character_id": target_id,
            "target_name": names.get(target_id, target_id),
            "relationship_type": _compact_text(raw.get("relationship_type"), 180),
            "relationship_context": _compact_text(raw.get("relationship_context"), 520),
            "current_dynamic": _compact_text(raw.get("current_dynamic"), 420),
            "behavioral_pattern": _compact_text(raw.get("behavioral_pattern"), 420),
            "beliefs_about_target": _as_list(raw.get("beliefs_about_target")),
            "unresolved_between_them": _as_list(raw.get("unresolved_between_them")),
            "dynamic_constraints": _as_list(raw.get("dynamic_constraints")),
            "interaction_hooks": _as_list(raw.get("interaction_hooks")),
            "status": raw.get("status") or "active",
            "last_changed_turn": raw.get("last_changed_turn"),
            "source": raw.get("source") or "profile",
        }
        relations.append({key: value for key, value in row.items() if value not in (None, "", [], {})})

    relations.sort(key=lambda row: (
        str(row.get("owner_name") or "").casefold(),
        str(row.get("target_name") or "").casefold(),
    ))
    return {
        "director_only": True,
        "directional": True,
        "always_read": True,
        "instruction": (
            "Это значимые NPC↔NPC связи. Направление важно: A→B и B→A могут различаться. "
            "Используй их как причинный источник поведения и самостоятельных сцен между NPC. "
            "Дружба не запрещает конфликт; бывшие могут иметь старые стычки; симпатия/романтика может проявляться между второстепенными персонажами. "
            "POV не обязан быть центром или вмешиваться. Связь не передаёт знания цели о мыслях/чувствах владельца автоматически."
        ),
        "relations": relations,
    }


def relations_for_character(network: Dict[str, Any], character_id: str) -> List[Dict[str, Any]]:
    rows = network.get("relations") if isinstance(network.get("relations"), list) else []
    result: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("owner_character_id") or "") == character_id:
            result.append(deepcopy(row))
        elif str(row.get("target_character_id") or "") == character_id:
            incoming = deepcopy(row)
            incoming["relation_direction"] = "incoming"
            result.append(incoming)
    return result[:12]


def apply_updates(
    state: Dict[str, Any],
    updates: Any,
    *,
    cards: List[Dict[str, Any]],
    resolve_character_id: Callable[[Iterable[Dict[str, Any]], Any], str | None],
    turn_number: int,
) -> Dict[str, Any]:
    if not isinstance(updates, list) or not updates:
        return deepcopy(state)

    result = deepcopy(state)
    store = result.get("npc_relationships")
    store = deepcopy(store) if isinstance(store, dict) else {}
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    valid_ids = {_card_id(card) for card in cards if _card_id(card)}
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = resolve_character_id(cards, raw.get("owner_character_id"))
        target_id = resolve_character_id(cards, raw.get("target_character_id"))
        owner_id = str(owner_id or "")
        target_id = str(target_id or "")
        if not owner_id or not target_id or owner_id == target_id:
            raise ValueError("NPC_RELATIONSHIP_INVALID_PAIR")
        if owner_id not in valid_ids or target_id not in valid_ids:
            raise ValueError("NPC_RELATIONSHIP_UNKNOWN_CHARACTER")
        if owner_id == pov_id or target_id == pov_id:
            raise ValueError("NPC_RELATIONSHIP_POV_NOT_ALLOWED")

        owner_bucket = store.setdefault(owner_id, {})
        existing = owner_bucket.get(target_id)
        relation = deepcopy(existing) if isinstance(existing, dict) else {
            "owner_character_id": owner_id,
            "target_character_id": target_id,
            "status": "active",
            "change_reasons": [],
        }

        for key in RELATION_TEXT_FIELDS:
            if key in raw and raw.get(key) is not None:
                relation[key] = _compact_text(raw.get(key), 700) or ""
        for key in RELATION_LIST_FIELDS:
            if key in raw and raw.get(key) is not None:
                relation[key] = _as_list(raw.get(key), 12)

        reason = _compact_text(raw.get("change_reason") or raw.get("reason"), 700)
        if reason:
            history = relation.get("change_reasons")
            history = list(history) if isinstance(history, list) else []
            history.append({"turn": int(turn_number), "reason": reason})
            relation["change_reasons"] = history[-20:]

        relation["owner_character_id"] = owner_id
        relation["target_character_id"] = target_id
        relation["last_changed_turn"] = int(turn_number)
        owner_bucket[target_id] = relation

    result["npc_relationships"] = store
    return result
