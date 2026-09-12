from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import relationship_runtime, session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 1

_META_KEYS = (
    "opinion",
    "current_dynamic",
    "beliefs_about_target",
    "unresolved_between_them",
    "relationship_type",
    "relationship_context",
)


def _error(code: str, message: str) -> None:
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _resolve_character_id(cards: Iterable[Dict[str, Any]], value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    needle = _norm(value)
    if not needle:
        return None
    for card in cards:
        cid = storage._card_id(card)
        if _norm(cid) == needle:
            return cid
        if any(_norm(alias) == needle for alias in storage._card_names(card)):
            return cid
    return None


def _relation_for(state: Dict[str, Any], owner_id: str) -> Dict[str, Any] | None:
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    doc = docs.get(owner_id) if isinstance(docs.get(owner_id), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    for relation in doc.get("relations", []) if isinstance(doc.get("relations"), list) else []:
        if isinstance(relation, dict) and str(relation.get("target_character_id") or "") == pov_id:
            return relation
    return None


def _relationship_index(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    result: Dict[str, Any] = {}
    for card in cards:
        cid = storage._card_id(card)
        if not cid or cid == pov_id:
            continue
        row = flat.get(cid) if isinstance(flat.get(cid), dict) else {}
        metrics = {
            str(label): value
            for label, value in row.items()
            if _is_number(value)
        }
        relation = _relation_for(state, cid) or {}
        entry: Dict[str, Any] = {
            "name": storage._card_name(card) or cid,
            "metrics": metrics,
        }
        current_dynamic = str(relation.get("current_dynamic") or "").strip()
        if current_dynamic:
            entry["current_dynamic"] = current_dynamic[:240]
        last_changed = int(relation.get("last_changed_turn", 0) or 0)
        if last_changed:
            entry["last_changed_turn"] = last_changed
        result[cid] = entry
    return result


def _participant_ids(
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    extracted: Dict[str, Any],
) -> set[str]:
    result = {str(value) for value in storage._present_character_ids(state_before) if value}

    def add(raw: Any) -> None:
        cid = _resolve_character_id(cards, raw)
        if cid:
            result.add(str(cid))

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
        "participants",
        "participant_ids",
        "character_id",
        "asked_by",
        "asked_to",
        "speaker",
        "listener",
        "said_by",
        "heard_by",
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


def _post_present_ids(
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    extracted: Dict[str, Any],
) -> set[str]:
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current_patch = state_patch.get("current") if isinstance(state_patch.get("current"), dict) else {}
    direct = current_patch.get("present_characters")
    if isinstance(direct, list):
        result: set[str] = set()
        for raw in direct:
            cid = _resolve_character_id(cards, raw)
            if cid:
                result.add(str(cid))
        return result

    result = {str(value) for value in storage._present_character_ids(state_before) if value}
    for row in extracted.get("presence_updates", []) if isinstance(extracted.get("presence_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        cid = _resolve_character_id(cards, row.get("character_id") or row.get("id") or row.get("name"))
        if not cid:
            continue
        action = str(row.get("action") or "").casefold().strip()
        if action == "enter":
            result.add(str(cid))
        elif action == "leave":
            result.discard(str(cid))
    return result


def _update_scope(raw: Dict[str, Any]) -> tuple[str, float]:
    scope = str(raw.get("change_scale") or "ordinary").casefold().strip()
    if scope == "ordinary":
        return scope, 3.0
    if scope == "timeskip":
        days = raw.get("elapsed_game_days")
        if not _is_number(days) or float(days) <= 0:
            _error(
                "RELATIONSHIP_TIMESKIP_DURATION_REQUIRED",
                "timeskip relationship change requires elapsed_game_days > 0.",
            )
        return scope, min(30.0, max(3.0, math.ceil(float(days)) * 3.0))
    if scope == "critical_event":
        return scope, 25.0
    _error(
        "RELATIONSHIP_CHANGE_SCALE_INVALID",
        "change_scale must be ordinary, timeskip or critical_event.",
    )
    raise AssertionError("unreachable")


def _numeric_baseline(state: Dict[str, Any], owner_id: str) -> Dict[str, tuple[str, float]]:
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    row = flat.get(owner_id) if isinstance(flat.get(owner_id), dict) else {}
    return {
        _norm(label): (str(label), float(value))
        for label, value in row.items()
        if _is_number(value)
    }


def _validate_update_rows(
    payload: Dict[str, Any],
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    updates = extracted.get("relationship_updates")
    if updates in (None, []):
        return {}
    if not isinstance(updates, list):
        _error("RELATIONSHIP_UPDATES_INVALID", "relationship_updates must be an array.")

    participants = _participant_ids(cards, state_before, extracted)
    by_owner: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for raw in updates:
        if not isinstance(raw, dict):
            _error("RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            _error("RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)

        dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), list) else []
        metadata_change = any(key in raw for key in _META_KEYS)
        baseline = _numeric_baseline(state_before, owner_id)
        changed = metadata_change
        owner_dims: Dict[str, Dict[str, Any]] = {}

        for dim in dimensions:
            if not isinstance(dim, dict):
                _error("RELATIONSHIP_UPDATES_INVALID", "Relationship dimension must be an object.")
            label = str(dim.get("label") or dim.get("key") or "").strip()
            if not label:
                _error("RELATIONSHIP_UPDATES_INVALID", "Relationship dimension requires label.")
            key = _norm(label)
            value = dim.get("value")
            delta = dim.get("delta")
            if not _is_number(value):
                _error("RELATIONSHIP_UPDATES_INVALID", f"{owner_id}: {label} requires numeric value.")

            if key in baseline:
                _, old_value = baseline[key]
                if delta is None:
                    if abs(float(value) - old_value) > 1e-9:
                        _error(
                            "RELATIONSHIP_DELTA_REQUIRED",
                            f"{owner_id}: existing metric {label} may change only through delta.",
                        )
                    delta_value = 0.0
                elif not _is_number(delta):
                    _error("RELATIONSHIP_UPDATES_INVALID", f"{owner_id}: {label} delta must be numeric.")
                else:
                    delta_value = float(delta)
                if abs(delta_value) > 1e-9:
                    changed = True
            else:
                delta_value = float(delta) if _is_number(delta) else None
                changed = True

            owner_dims[key] = {
                "label": label,
                "value": float(value),
                "delta": delta_value,
                "is_new": key not in baseline,
            }

        if changed and owner_id not in participants:
            _error(
                "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                "Relationship may change only for an NPC who concretely participated in this turn. "
                "A mere name mention, thread or cast relevance is not participation.",
            )

        if changed:
            reason = str(raw.get("reason") or "").strip()
            if not reason:
                _error(
                    "RELATIONSHIP_CHANGE_REASON_REQUIRED",
                    f"{owner_id}: every real relationship change requires a concrete reason.",
                )
            scope, max_delta = _update_scope(raw)
            for dim in owner_dims.values():
                delta_value = dim.get("delta")
                if delta_value is not None and abs(float(delta_value)) > max_delta + 1e-9:
                    _error(
                        "RELATIONSHIP_DELTA_OUT_OF_RANGE",
                        f"{owner_id}: {dim['label']} delta {float(delta_value):g} is too large for "
                        f"{scope}; allowed magnitude is {max_delta:g}.",
                    )

        by_owner[owner_id] = owner_dims

    return by_owner


def _validate_footer(
    payload: Dict[str, Any],
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    explicit: Dict[str, Dict[str, Dict[str, Any]]],
) -> None:
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    final_present = _post_present_ids(cards, state_before, extracted)
    pov = state_before.get("pov") if isinstance(state_before.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    footer = relationship_runtime._parse_footer(
        str(payload.get("scene_output") or ""),
        cards=cards,
        resolve_character_id=_resolve_character_id,
    )

    for owner_id in final_present:
        if not owner_id or owner_id == pov_id:
            continue
        baseline = _numeric_baseline(state_before, owner_id)
        explicit_dims = explicit.get(owner_id, {})
        if not baseline and not explicit_dims:
            continue

        incoming = footer.get(owner_id)
        if not incoming:
            _error(
                "RELATIONSHIP_FOOTER_INCOMPLETE",
                f"{owner_id}: visible footer must show every established relationship metric for a present NPC.",
            )
        incoming_by_norm = {
            _norm(item.get("label") or item.get("key")): item
            for item in incoming
            if isinstance(item, dict)
        }

        required_keys = set(baseline) | set(explicit_dims)
        missing = [
            (baseline.get(key) or (explicit_dims[key]["label"], 0))[0]
            for key in required_keys
            if key not in incoming_by_norm
        ]
        if missing:
            _error(
                "RELATIONSHIP_FOOTER_INCOMPLETE",
                f"{owner_id}: visible footer omitted established metrics: {', '.join(missing)}.",
            )

        for key in required_keys:
            item = incoming_by_norm[key]
            value = item.get("value")
            if not _is_number(value):
                _error("RELATIONSHIP_FOOTER_INCOMPLETE", f"{owner_id}: footer metric must be numeric.")
            if key in baseline:
                _, old_value = baseline[key]
            else:
                old_value = None
            update = explicit_dims.get(key)
            if update and update.get("delta") is not None and old_value is not None:
                delta = float(update["delta"])
                expected = max(0.0, min(100.0, old_value + delta))
                visible_delta = item.get("delta")
                if not _is_number(visible_delta) or abs(float(visible_delta) - delta) > 1e-9:
                    _error(
                        "RELATIONSHIP_DELTA_REQUIRED",
                        f"{owner_id}: changed metric {update['label']} must show final/delta in the footer.",
                    )
                if abs(float(value) - expected) > 1e-9:
                    _error(
                        "RELATIONSHIP_ARITHMETIC_MISMATCH",
                        f"{owner_id}: {update['label']} footer value must equal saved value + delta.",
                    )
            elif update and update.get("is_new"):
                if abs(float(value) - float(update["value"])) > 1e-9:
                    _error(
                        "RELATIONSHIP_ARITHMETIC_MISMATCH",
                        f"{owner_id}: new metric {update['label']} must match relationship_updates.",
                    )
            elif old_value is not None:
                visible_delta = item.get("delta")
                if visible_delta is not None and (not _is_number(visible_delta) or abs(float(visible_delta)) > 1e-9):
                    _error(
                        "RELATIONSHIP_CHANGE_REASON_REQUIRED",
                        f"{owner_id}: footer cannot change {baseline[key][0]} without a causal relationship_updates row.",
                    )
                if abs(float(value) - old_value) > 1e-9:
                    _error(
                        "RELATIONSHIP_CHANGE_REASON_REQUIRED",
                        f"{owner_id}: footer cannot change {baseline[key][0]} without a causal relationship_updates row.",
                    )


def _validate_relationship_commit(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    explicit = _validate_update_rows(payload, cards=cards, state_before=state)
    _validate_footer(payload, cards=cards, state_before=state, explicit=explicit)


def _rewrite_packet(session_id: str, base_result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base_result
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base_result

        state = storage._read_json(root / "state.json", {})
        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        context["relationship_index"] = {
            "direction": "NPC -> POV",
            "always_read": True,
            "characters": _relationship_index(state, cards),
            "instruction": (
                "MANDATORY EVERY TURN. This compact index is the current directed relationship baseline for every registered NPC, "
                "including offscreen characters. Zero is a real saved value, not absence. Combine these values with that NPC's "
                "character, knowledge, current opinion, goals and circumstances. Strong positive or conflictual bonds may increase "
                "initiative/frequency, but the form of approach, avoidance, jealousy, suspicion, help or conflict must follow the "
                "specific NPC rather than a universal table. Never infer POV->NPC feelings from this NPC->POV index."
            ),
        }

        policy = context.get("relationship_policy") if isinstance(context.get("relationship_policy"), dict) else {}
        policy.update(
            {
                "source_of_truth": "persistent relationship_documents synchronized to relationships",
                "common_index_path": "relationship_index.characters",
                "common_index_always_read": True,
                "footer_is_display_only": True,
                "unchanged_metrics_must_remain_visible_for_present_npc": True,
                "zero_is_persistent_value": True,
                "change_requires_reason": True,
                "change_scales": {
                    "ordinary": "normally up to +/-3 per metric for one ordinary turn",
                    "timeskip": "up to 3 points per elapsed_game_day, capped at 30; elapsed_game_days is required",
                    "critical_event": "up to +/-25 for a genuinely relationship-changing event",
                },
                "instruction": (
                    "No reason = no relationship change. Most turns may change nothing, and a real scene may change only one metric. "
                    "Reaction magnitude and direction depend on the individual NPC. For a changed existing metric, relationship_updates "
                    "must contain delta and reason; visible footer shows final/delta. Large jumps require timeskip or critical_event."
                ),
            }
        )
        context["relationship_policy"] = policy

        persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
        persistence["relationship_updates"] = {
            "optional": True,
            "when": "Only when this turn causally changes numeric relationship state or relationship opinion/dynamic.",
            "ordinary_example": {
                "character_id": "npc_id",
                "reason": "Concrete event and why this NPC reacted to it",
                "change_scale": "ordinary",
                "dimensions": [{"label": "доверие", "value": 12, "delta": 1}],
            },
            "timeskip_example": {
                "character_id": "npc_id",
                "reason": "Five days of repeated close contact",
                "change_scale": "timeskip",
                "elapsed_game_days": 5,
                "dimensions": [{"label": "привязанность", "value": 20, "delta": 10}],
            },
            "critical_example": {
                "character_id": "npc_id",
                "reason": "Serious betrayal of a secret this NPC personally entrusted to POV",
                "change_scale": "critical_event",
                "dimensions": [{"label": "доверие", "value": 25, "delta": -15}],
            },
            "instruction": (
                "Existing metric changes require delta; unchanged metrics are omitted from relationship_updates but remain persistent. "
                "Every real change requires reason. Mere mention/thread/cast relevance is not participation. Direct current-turn contact "
                "such as a real conversation, message or call counts when the extracted memory/participation evidence identifies that NPC."
            ),
        }
        context["persistence_contract"] = persistence

        living = context.get("living_world") if isinstance(context.get("living_world"), dict) else {}
        model = living.get("relationship_model") if isinstance(living.get("relationship_model"), dict) else {}
        model["behavior_rule"] = (
            "Relationship values are behavioral inputs, not automatic actions. High attachment/closeness/sympathy may support seeking "
            "contact; high resentment/jealousy/suspicion may support confrontation, testing, avoidance or interference depending on "
            "character. Character personality decides which response is natural."
        )
        living["relationship_model"] = model
        context["living_world"] = living

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["relationship_policy_version"] = _VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base_result)
        result.update(
            {
                "chunk_count": len(chunks),
                "total_chars": len(text),
                "first_chunk_included": True,
                "chunk_index": 0,
                "content": chunks[0],
                "all_chunks_read": len(chunks) == 1,
                "next_chunk_index": None if len(chunks) == 1 else 1,
                "relationship_index": True,
            }
        )
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_relationship_commit(session_id, payload)
    return dict(_ORIGINAL_COMMIT(session_id, payload))


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
