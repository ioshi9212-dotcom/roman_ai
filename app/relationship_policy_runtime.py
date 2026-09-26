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
_VERSION = 2

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


def _relationship_index(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Tiny always-read NPC->POV numeric index. Names/dossiers stay in character_registry/bundles."""
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    flat = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    result: Dict[str, Dict[str, float]] = {}
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
        if metrics:
            result[cid] = metrics
    return result

def _participant_ids(
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
    extracted: Dict[str, Any],
) -> set[str]:
    result = {str(value) for value in storage._scene_participant_ids(state_before) if value}

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

    remote = current_patch.get("remote_characters")
    if isinstance(remote, list):
        for value in remote:
            add(value)
    elif remote not in (None, "", {}, []):
        add(remote)

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


def _explicit_input_character_ids(cards: List[Dict[str, Any]], user_input: Any) -> set[str]:
    text = _norm(user_input)
    if not text:
        return set()
    result: set[str] = set()
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        aliases = [cid, *storage._card_names(card)]
        if any(_norm(alias) and _norm(alias) in text for alias in aliases):
            result.add(str(cid))
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


def _clamp_relationship_value(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _reviewed_footer_delta_fallbacks(
    payload: Dict[str, Any],
    *,
    cards: List[Dict[str, Any]],
    state_before: Dict[str, Any],
) -> Dict[str, Any]:
    """Recover an omitted relationship_update from a reviewed, arithmetically valid footer delta.

    Persistent state remains authoritative. This bridge accepts only an existing metric, a non-zero
    ordinary delta within +/-3, a final footer value equal to saved+delta, and a concretely
    participating NPC who is still present at scene end. Departing/remote changes still require
    explicit relationship_updates. This remains compatibility-safe
    for older clients because an explicit, arithmetically valid /delta is itself required.
    """
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}

    footer = relationship_runtime._parse_footer(
        str(result.get("scene_output") or ""),
        cards=cards,
        resolve_character_id=_resolve_character_id,
    )
    if not footer:
        return result

    participants = _participant_ids(cards, state_before, extracted)
    final_present = _post_present_ids(cards, state_before, extracted)
    rows = deepcopy(extracted.get("relationship_updates"))
    if not isinstance(rows, list):
        rows = []

    row_by_owner: Dict[str, Dict[str, Any]] = {}
    explicit_labels: Dict[str, set[str]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            continue
        owner_id = str(owner_id)
        row_by_owner.setdefault(owner_id, raw)
        for dim in raw.get("dimensions", []) if isinstance(raw.get("dimensions"), list) else []:
            if not isinstance(dim, dict):
                continue
            label = _norm(dim.get("label") or dim.get("key"))
            if label:
                explicit_labels.setdefault(owner_id, set()).add(label)

    for owner_id, dimensions in footer.items():
        owner_id = str(owner_id)
        if owner_id not in participants or owner_id not in final_present:
            continue
        baseline = _numeric_baseline(state_before, owner_id)
        if not baseline:
            continue

        recovered: List[Dict[str, Any]] = []
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            key = _norm(dim.get("label") or dim.get("key"))
            if not key or key not in baseline or key in explicit_labels.get(owner_id, set()):
                continue
            delta = dim.get("delta")
            value = dim.get("value")
            if not _is_number(delta) or not _is_number(value):
                continue
            delta_value = float(delta)
            if abs(delta_value) < 1e-9 or abs(delta_value) > 3.0:
                continue
            saved_label, old_value = baseline[key]
            expected = _clamp_relationship_value(old_value + delta_value)
            if abs(float(value) - expected) > 1e-9:
                continue
            recovered.append({
                "label": saved_label,
                "value": expected,
                "delta": delta_value,
            })

        if not recovered:
            continue

        target = row_by_owner.get(owner_id)
        if target is None:
            target = {
                "character_id": owner_id,
                "reason": "Compatibility fallback: the scene footer recorded an explicit causal delta.",
                "change_scale": "ordinary",
                "dimensions": [],
            }
            rows.append(target)
            row_by_owner[owner_id] = target
        else:
            target.setdefault(
                "reason",
                "Compatibility fallback: the scene footer recorded an explicit causal delta.",
            )
            target.setdefault("change_scale", "ordinary")

        target_dims = target.get("dimensions")
        if not isinstance(target_dims, list):
            target_dims = []
            target["dimensions"] = target_dims
        target_dims.extend(recovered)

    extracted = deepcopy(extracted)
    extracted["relationship_updates"] = rows
    result["extracted"] = extracted
    return result


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
    explicitly_named = _explicit_input_character_ids(cards, payload.get("user_input"))
    by_owner: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for raw in updates:
        if not isinstance(raw, dict):
            _error("RELATIONSHIP_UPDATES_INVALID", "Each relationship update must be an object.")
        owner_id = _resolve_character_id(cards, raw.get("character_id"))
        if not owner_id:
            _error("RELATIONSHIP_UPDATES_INVALID", "Unknown character_id in relationship_updates.")
        owner_id = str(owner_id)

        dimensions = raw.get("dimensions") if isinstance(raw.get("dimensions"), list) else []
        metadata_change = any(raw.get(key) is not None for key in _META_KEYS)
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
            if key not in baseline and not relationship_runtime.valid_dimension_label(label):
                _error(
                    "RELATIONSHIP_DIMENSION_LABEL_INVALID",
                    f"{owner_id}: new relationship metric {label!r} must be a short single-line label without footer separators.",
                )
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

        new_keys = {key for key, dim in owner_dims.items() if dim.get("is_new")}
        if len(baseline) + len(new_keys) > relationship_runtime.MAX_DIMENSIONS:
            _error(
                "RELATIONSHIP_DIMENSION_LIMIT",
                f"{owner_id}: relationship dimension limit is {relationship_runtime.MAX_DIMENSIONS}; "
                "do not silently drop a new metric.",
            )

        if changed:
            reason = str(raw.get("reason") or "").strip()
            if not reason:
                _error(
                    "RELATIONSHIP_CHANGE_REASON_REQUIRED",
                    f"{owner_id}: every real relationship change requires a concrete reason.",
                )
            scope, max_delta = _update_scope(raw)
            aggregate_timeskip = scope == "timeskip" and owner_id in explicitly_named
            if owner_id not in participants and not aggregate_timeskip:
                _error(
                    "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC",
                    "Relationship may change only for an NPC who concretely participated in this turn. "
                    "Exception: a timeskip may aggregate repeated contact/avoidance for an NPC explicitly named by the player.",
                )
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
    """Visible relationship footer is presentation only and never a commit gate.

    Canonical NPC->POV numbers are validated from persistent state plus causal
    relationship_updates. A stale, partial or cosmetically wrong footer must not
    block a gameplay turn or mutate relationship canon.
    """
    return

def _validate_relationship_commit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    prepared = _reviewed_footer_delta_fallbacks(
        payload,
        cards=cards,
        state_before=state,
    )
    explicit = _validate_update_rows(prepared, cards=cards, state_before=state)
    _validate_footer(prepared, cards=cards, state_before=state, explicit=explicit)
    return prepared


def _rewrite_packet(session_id: str, base_result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base_result
        if packet.get("relationship_policy_version") == _VERSION:
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
            "instruction": "Читай каждый ход; 0 сохраняется.",
        }

        previous_policy = context.get("relationship_policy") if isinstance(context.get("relationship_policy"), dict) else {}
        context["relationship_policy"] = {
            "authoritative_start_snapshot": previous_policy.get("authoritative_start_snapshot", {}),
            "source_of_truth": "persistent relationship_documents synchronized to relationships",
            "common_index_path": "relationship_index.characters",
            "required_review_every_turn": True,
            "footer_is_display_only": True,
            "footer_explicit_delta_fallback": True,
            "footer_is_transaction_gate": False,
            "footer_required_for_every_present_npc": False,
            "fresh_baseline_required": False,
            "new_dimensions_may_be_appended": True,
            "open_dimension_vocabulary": True,
            "zero_dimensions_may_be_hidden": False,
            "change_requires_reason": True,
            "limits": {"ordinary": 3, "timeskip_per_day": 3, "timeskip_cap": 30, "critical_event": 25},
            "instruction": (
                "После всей сцены обязательно переоцени отношение каждого участвовавшего NPC к POV. "
                "Не замораживай одни и те же показатели на многих ходах, если отношения явно развиваются или ухудшаются; "
                "если появляется новое устойчивое качество отношений, которого нет среди сохранённых шкал, добавь новую dimension "
                "с конкретным reason и начальным value. Она не обязана быть в стартовом наборе. Не создавай синонимы уже существующих "
                "шкал и не превращай краткую эмоцию в постоянную dimension. "
                "Не меняй показатели механически без реального основания. Основной канал изменения — causal relationship_updates с reason+delta. "
                "Footer остаётся display-only, но backend может восстановить забытый ordinary update только из явного /delta, "
                "если final=saved+delta; кривой, большой или неучаствующий delta игнорируется."
            ),
        }

        persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
        persistence["relationship_updates"] = {
            "optional": True,
            "review_required": True,
            "when": "После обязательной проверки: только real causal NPC->POV change.",
            "fields": "character_id, reason, change_scale, elapsed_game_days(timeskip), dimensions[label,value,delta(existing)]",
            "instruction": (
                "Existing metric → delta+reason. New durable metric → initial value+reason; новый label разрешён "
                "и может появиться на любом ходу, если это самостоятельное устойчивое качество, а не синоним/мимолётная эмоция. "
                "ordinary<=3; timeskip 3/day cap30; critical_event<=25. "
                "Явный корректный footer /delta — только аварийный fallback, не замена relationship_updates."
            ),
        }
        context["persistence_contract"] = persistence

        living = context.get("living_world") if isinstance(context.get("living_world"), dict) else {}
        model = living.get("relationship_model") if isinstance(living.get("relationship_model"), dict) else {}
        model["behavior_rule"] = "Отношения влияют на поведение вместе с характером и ситуацией."
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
    prepared = _validate_relationship_commit(session_id, payload)
    return dict(_ORIGINAL_COMMIT(session_id, prepared))


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
