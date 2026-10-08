from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from fastapi import HTTPException

from . import npc_intent, relationship_file_runtime, storage, story_thread


_VERSION = 1
_TIME_SKIP_RE = re.compile(
    r"(?iu)(?:"
    r"\bспат\w*|\bуснут\w*|\bпоспат\w*|\bдремат\w*|\bждат\w*|\bожидат\w*|"
    r"\bдо\s+(?:утра|вечера|ночи|пробуждения|рассвета|обеда|завтра)\b|"
    r"\bчерез\s+\d+\s*(?:минут|час|часа|часов|дн\w*)\b|"
    r"\bпромотат\w*\s+время|\bпереждат\w*|"
    r"\bзанимат\w*\s+(?:обычн\w*\s+)?дел\w*\s+до\b|"
    r"\bвернут\w*\s+к\s+(?:обычн\w*\s+)?дел\w*\s+до\b"
    r")"
)
_EXPLICIT_UNEVENTFUL_RE = re.compile(
    r"(?iu)(?:"
    r"\bбез\s+(?:событий|происшествий|приключений)\b|"
    r"\bпусть\s+ничего\s+не\s+(?:происходит|случается|случится)\b|"
    r"\bничего\s+не\s+(?:должно\s+)?(?:происходить|случаться)\b|"
    r"\bбез\s+каких[- ]либо\s+событий\b"
    r")"
)

_ROUTINE_RE = re.compile(
    r"(?iu)(?:"
    r"\bспал\w*|\bуснул\w*|\bпроснул\w*|\bпоспал\w*|\bдремал\w*|"
    r"\bждал\w*|\bожидал\w*|\bпоел\w*|\bел\b|\bпил\w*|\bвыпил\w*|"
    r"\bпоехал\w*|\bехал\w*|\bприехал\w*|\bвернул\w*\s+домой|"
    r"\bпроверил\w*\s+(?:телефон|часы|почту)\b|"
    r"\bвремя\s+(?:прошло|идет|идёт)\b|\bнаступил\w*\s+(?:утро|вечер|ночь)\b|"
    r"\bпогода\b"
    r")"
)
_MEANINGFUL_MARKER_RE = re.compile(
    r"(?iu)(?:"
    r"\bсообщен\w*|\bзвон\w*|\bписьм\w*|\bстук\w*|\bсигнал\w*|"
    r"\bугроз\w*|\bопасн\w*|\bнапал\w*|\bатак\w*|\bисчез\w*|\bпояв\w*|"
    r"\bобнаруж\w*|\bнаш[её]л\w*|\bзаметил\w*|\bслед\w*|\bошибк\w*|"
    r"\bавари\w*|\bкров\w*|\bсломал\w*|\bвош[её]л\w*|\bприш[её]л\w*|"
    r"\bдедлайн\w*|\bсрок\w*|\bуслови\w*|\bвозможност\w*|\bшанс\w*"
    r")"
)
_PASSIVE_END_RE = re.compile(
    r"(?iu)(?:"
    r"\bничего\s+не\s+(?:произошло|случилось)\b|"
    r"\bвс[её]\s+(?:было\s+)?(?:тихо|спокойно|без\s+изменений)\b|"
    r"\bпросто\s+(?:уснул|лег\s+спать|ждал|проснулся)\b|"
    r"\bдень\s+(?:только\s+)?начинал\w*|"
    r"\bещ[её]\s+не\s+знал\w*\s*,?\s+что\b"
    r")"
)

_KIND_TO_ENDINGS = {
    "new_information": {"new_fact", "concrete_next_pressure", "conflict_change"},
    "external_event": {"incoming_contact", "concrete_next_pressure", "new_constraint", "new_opportunity", "new_threat", "conflict_change"},
    "npc_action": {"intent_action", "incoming_contact", "concrete_next_pressure", "conflict_change"},
    "thread_state_change": {"concrete_next_pressure", "new_constraint", "new_opportunity", "new_threat", "conflict_change"},
    "relationship_shift": {"relationship_shift", "conflict_change", "concrete_next_pressure"},
    "new_constraint": {"new_constraint", "concrete_next_pressure", "conflict_change"},
    "new_opportunity": {"new_opportunity", "concrete_next_pressure"},
    "new_threat": {"new_threat", "concrete_next_pressure", "conflict_change"},
    "unfinished_action_specific": {"concrete_next_pressure", "conflict_change", "new_constraint", "new_opportunity"},
}

_ALLOWED_KINDS = set(_KIND_TO_ENDINGS)
_ALLOWED_ENDINGS = set().union(*_KIND_TO_ENDINGS.values())


_TARGET_KIND_PREFIXES = {
    "thread:": {"thread_state_change", "external_event"},
    "intent:": {"npc_action", "external_event"},
    "relationship:": {"relationship_shift"},
    "unfinished:": {"unfinished_action_specific"},
    "world:": {"new_information", "external_event", "npc_action", "new_constraint", "new_opportunity", "new_threat"},
    "cast:": {"npc_action", "external_event"},
}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _text_from_row(row: Any) -> str:
    if isinstance(row, str):
        return " ".join(row.split())
    if not isinstance(row, dict):
        return ""
    for key in ("text", "summary", "event", "description", "fact", "progress_summary", "reason"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return " ".join(
        str(value)
        for value in row.values()
        if isinstance(value, (str, int, float)) and str(value).strip()
    )


def _routine_only(text: str) -> bool:
    normalized = _norm(text)
    if not normalized:
        return True
    if _MEANINGFUL_MARKER_RE.search(normalized):
        return False
    return _ROUTINE_RE.search(normalized) is not None


def _priority(value: Any) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return {"critical": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}.get(_norm(value), 0)


def _thread_candidates(state: Dict[str, Any], current_turn: int) -> List[Dict[str, Any]]:
    rows = story_thread.active_threads(state)
    ranked: List[tuple[int, Dict[str, Any]]] = []
    for thread_id, row in rows.items():
        if not isinstance(row, dict):
            continue
        last_turn = 0
        for key in ("last_progress_turn", "last_turn", "updated_turn", "created_turn", "start_turn"):
            try:
                if row.get(key) not in (None, ""):
                    last_turn = int(row.get(key))
                    break
            except (TypeError, ValueError):
                pass
        age = max(0, current_turn - last_turn) if last_turn else current_turn
        item = {
            "target_id": f"thread:{thread_id}",
            "source": "story_thread",
            "thread_id": str(thread_id),
            "priority": row.get("priority"),
            "turns_since_progress": age,
        }
        ranked.append((_priority(row.get("priority")) + age, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {key: value for key, value in item.items() if value not in (None, "", [], {})}
        for _, item in ranked[:8]
    ]


def _intent_candidates(state: Dict[str, Any], cards: List[Dict[str, Any]], current_turn: int) -> List[Dict[str, Any]]:
    ids = [storage._card_id(card) for card in cards if storage._card_id(card)]
    scoped = npc_intent.active_intents_for(
        state,
        ids,
        current_turn=current_turn,
        max_per_character=2,
    )
    ranked: List[tuple[int, Dict[str, Any]]] = []
    for cid, rows in scoped.items():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("eligible_now") is not True:
                continue
            intent_id = str(row.get("intent_id") or row.get("id") or "").strip()
            if not intent_id:
                continue
            item = {
                "target_id": f"intent:{cid}:{intent_id}",
                "source": "npc_intent",
                "character_id": str(cid),
                "intent_id": intent_id,
                "priority": row.get("priority"),
                "turns_since_pursued": row.get("turns_since_pursued"),
            }
            ranked.append((
                _priority(row.get("priority")) + int(row.get("turns_since_pursued") or 0),
                item,
            ))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {key: value for key, value in item.items() if value not in (None, "", [], {})}
        for _, item in ranked[:8]
    ]


def _relationship_candidates(
    context: Dict[str, Any],
    *,
    pov_id: str,
) -> List[Dict[str, Any]]:
    scene = context.get("scene_presence") if isinstance(context.get("scene_presence"), dict) else {}
    ids = [
        *[str(value) for value in scene.get("present_character_ids", []) if value],
        *[str(value) for value in scene.get("remote_character_ids", []) if value],
    ]
    result: List[Dict[str, Any]] = []
    for cid in dict.fromkeys(ids):
        if not cid or cid == pov_id:
            continue
        result.append({
            "target_id": f"relationship:{cid}",
            "source": "relationship",
            "character_id": cid,
        })
    return [{k: v for k, v in row.items() if v not in (None, "", {}, [])} for row in result[:6]]


def _cast_candidates(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    registry = context.get("cast_registry") if isinstance(context.get("cast_registry"), dict) else {}
    rows = registry.get("characters") if isinstance(registry.get("characters"), list) else []
    if not any(isinstance(row, dict) and row.get("offscreen_can_initiate") is True for row in rows):
        return []
    return [{"target_id": "cast:independent", "source": "independent_cast_goal"}]


def build_contract(
    *,
    state: Dict[str, Any],
    context: Dict[str, Any],
    user_input: str,
    cards: List[Dict[str, Any]],
    current_turn: int,
) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    unfinished = current.get("unfinished_actions")
    unfinished = unfinished if isinstance(unfinished, list) else []

    targets: List[Dict[str, Any]] = []
    if unfinished:
        targets.append({
            "target_id": "unfinished:current",
            "source": "unfinished_actions",
            "item_count": len([value for value in unfinished if str(value).strip()]),
        })
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    # Current-scene relationships stay near the front so a large backlog of
    # threads/intents cannot crowd out a legitimate social progression target.
    targets.extend(_relationship_candidates(context, pov_id=pov_id))
    targets.extend(_cast_candidates(context))
    targets.extend(_intent_candidates(state, cards, current_turn))
    targets.extend(_thread_candidates(state, current_turn))
    world_target = {
        "target_id": "world:emergent",
        "source": "causal_world_consequence",
        "guidance": (
            "Fallback only when no existing thread/intent/unfinished action/relationship can naturally advance. "
            "Do not manufacture a genre anomaly merely to satisfy progression."
        ),
    }

    mapping = context.get("player_input_map") if isinstance(context.get("player_input_map"), dict) else {}
    stage_rows = mapping.get("stage_directions") if isinstance(mapping.get("stage_directions"), list) else []
    stage_text = " ".join(str(value) for value in stage_rows if str(value).strip())
    time_skip = _TIME_SKIP_RE.search(stage_text) is not None
    explicit_quiet = _EXPLICIT_UNEVENTFUL_RE.search(stage_text) is not None
    return {
        "version": _VERSION,
        "mandatory": True,
        "required_target_count": 0 if explicit_quiet else 1,
        "time_skip_requested": time_skip,
        "explicit_uneventful_downtime": explicit_quiet,
        "eligible_targets": [*targets[:17], world_target],
        "cast_character_ids": [str(row["character_id"]) for row in context.get("cast_registry", {}).get("characters", []) if isinstance(row, dict) and row.get("offscreen_can_initiate") is True and row.get("character_id")],
        "selection_sources": [
            "scene_state.current.unfinished_actions",
            "relationship_lens",
            "npc_active_intents/cast_registry.active_intents",
            "active_threads",
            "cast_registry.characters",
        ],
        "proof_required_in_commit": not explicit_quiet,
        "proof_fields": [
            "target",
            "kind",
            "action",
            "end_state_change",
            "ending_kind",
            "ending_evidence_text",
        ],
        "independent_cast_proof_rule": "cast:independent needs scene_progression.character_id and evidenced NPC action; no POV knowledge transfer.",
        "meaningful_progress_rule": (
            "Every gameplay turn must produce at least one meaningful world/story/relationship state change unless the user explicitly requests uneventful downtime. "
            "Movement, sleeping, eating, checking devices, weather or passage of time alone do not count."
        ),
        "time_skip_rule": (
            "A time skip is not permission for an empty interval. Advance at least one eligible offscreen thread, intent, consequence or relationship during the skipped interval "
            "and end at the first meaningful event, discovery, incoming contact, environmental/world change, NPC action or consequence that reaches the POV. "
            "Only explicit uneventful downtime may remain uneventful."
        ),
        "ending_rule": (
            "The final beat must leave the situation materially different from the opening beat. "
            "Do not end on nothing happened, generic reflection, routine completion or a menu of future possibilities. "
            "A hook must be backed by a real persisted event/state change, not an ominous sentence."
        ),
    }


def _packet_contract(root, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or packet.get("progression_review_required") is not True:
        return None
    if str(packet.get("packet_id") or "") != str(payload.get("packet_id") or ""):
        return None
    try:
        context = __import__("json").loads("".join(str(chunk) for chunk in packet.get("chunks", [])))
    except Exception:
        return None
    if not isinstance(context, dict):
        return None
    contract = context.get("progression_contract")
    return contract if isinstance(contract, dict) else None


def _relationship_changed_refs(root, payload: Dict[str, Any]) -> set[str]:
    after_store = payload.get("_relationships_after")
    if not isinstance(after_store, dict):
        return set()
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    before_store = relationship_file_runtime.load(
        root,
        cards=cards,
        state=state,
        pov_id=str(pov.get("character_id") or ""),
    )
    refs: set[str] = set()
    for raw in extracted.get("relationship_updates", []) if isinstance(extracted.get("relationship_updates"), list) else []:
        if not isinstance(raw, dict):
            continue
        cid = relationship_file_runtime._resolve_character_id(cards, raw.get("character_id"))
        if not cid:
            continue
        before = relationship_file_runtime.character_relation(before_store, str(cid)) or {}
        after = relationship_file_runtime.character_relation(after_store, str(cid)) or {}
        if before != after:
            refs.add(f"relationship:{cid}")
    return refs


def _post_unfinished_actions(root, payload: Dict[str, Any]) -> tuple[List[Any], List[Any]]:
    state = storage._read_json(root / "state.json", {})
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    before = current.get("unfinished_actions") if isinstance(current.get("unfinished_actions"), list) else []
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    post = storage._deep_merge(state, patch)
    current_after = post.get("current") if isinstance(post.get("current"), dict) else {}
    after = current_after.get("unfinished_actions") if isinstance(current_after.get("unfinished_actions"), list) else []
    return before, after


def _new_remote_contact(state: Dict[str, Any], extracted: Dict[str, Any], pov_id: str) -> bool:
    start_remote = {
        str(value)
        for value in storage._remote_character_ids(state)
        if value and str(value) != pov_id
    }
    rows = extracted.get("dialogue_memory_add")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or _norm(row.get("mode")) != "remote":
            continue
        participants = row.get("participants") or row.get("participant_ids") or []
        if isinstance(participants, str):
            participants = [participants]
        counterpart_ids = {
            str(value)
            for value in participants if value and str(value) != pov_id
        }
        if counterpart_ids - start_remote:
            return True
    return False


def _collect_evidence(root, payload: Dict[str, Any]) -> Dict[str, Any]:
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    kinds: set[str] = set()
    refs: set[str] = set()

    active_thread_ids = set(story_thread.active_threads(state))
    for row in extracted.get("story_thread_updates", []) if isinstance(extracted.get("story_thread_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        operation = _norm(row.get("operation") or "upsert")
        if row.get("progressed_now") is True or operation in {"resolve", "abandon"}:
            thread_id = str(row.get("thread_id") or "").strip()
            if thread_id:
                refs.add(f"thread:{thread_id}")
                if thread_id not in active_thread_ids:
                    refs.add("world:emergent")
            kinds.update({"thread_state_change", "external_event"})

    existing_intents = npc_intent.normalise_store(state)
    existing_intent_ids = {
        (str(cid), str(item.get("intent_id") or ""))
        for cid, rows in existing_intents.items()
        for item in rows if isinstance(item, dict) and item.get("intent_id")
    }
    for row in extracted.get("npc_intent_updates", []) if isinstance(extracted.get("npc_intent_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        operation = _norm(row.get("operation") or "upsert")
        if row.get("pursued_now") is True or operation in {"resolve", "abandon"}:
            cid = str(row.get("character_id") or "").strip()
            intent_id = str(row.get("intent_id") or "").strip()
            if cid and intent_id:
                refs.add(f"intent:{cid}:{intent_id}")
                if (cid, intent_id) not in existing_intent_ids:
                    refs.add("world:emergent")
            kinds.update({"npc_action", "external_event"})

    # Cast agency may create its first real action without a pre-existing intent.
    # Only accept concrete evidence attributed to that character, never its
    # card/goals or POV knowledge as proof that something actually happened.
    cast_ids = {
        storage._card_id(row)
        for row in storage._load_cards(root, storage._read_json(root / "source.json", {}))
        if isinstance(row, dict) and storage._card_id(row)
    }
    for row in extracted.get("npc_intent_updates", []) if isinstance(extracted.get("npc_intent_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or "")
        if cid in cast_ids and row.get("pursued_now") is True and row.get("intent_id"):
            refs.add(f"cast:{cid}")
            kinds.update({"npc_action", "external_event"})
    for row in extracted.get("presence_updates", []) if isinstance(extracted.get("presence_updates"), list) else []:
        if isinstance(row, dict) and str(row.get("character_id") or "") in cast_ids and _norm(row.get("action")) == "enter":
            refs.add(f"cast:{row['character_id']}")
            kinds.update({"npc_action", "external_event"})

    before_unfinished, after_unfinished = _post_unfinished_actions(root, payload)
    if before_unfinished != after_unfinished:
        refs.add("unfinished:current")
        kinds.add("unfinished_action_specific")

    relationship_refs = _relationship_changed_refs(root, payload)
    if relationship_refs:
        refs.update(relationship_refs)
        kinds.add("relationship_shift")

    chronology_rows = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
    for row in chronology_rows:
        if not isinstance(row, dict):
            continue
        text = _text_from_row(row)
        importance = _norm(row.get("importance"))
        consequences = row.get("consequences") if isinstance(row.get("consequences"), list) else []
        evidenced = (
            importance in {"major", "anchor", "critical"}
            or row.get("time_critical") is True
            or any(str(value).strip() for value in consequences)
        )
        if text and evidenced and not _routine_only(text):
            refs.add("world:emergent")
            kinds.update({"external_event", "new_information"})
            # A named, consequential action can advance a registered NPC's own
            # offscreen line without inventing an npc_intent or bringing that
            # character into POV's scene.
            actor = str(row.get("actor_character_id") or "").strip()
            if actor in cast_ids and actor != pov_id:
                refs.add(f"cast:{actor}")
                kinds.update({"npc_action", "external_event"})

    # Personal-memory writes are persistence of what was learned, not proof that
    # the learned fact was story-significant. Meaningful discoveries must also
    # change a thread/intent/relationship or be recorded as consequential chronology.

    presence_rows = extracted.get("presence_updates") if isinstance(extracted.get("presence_updates"), list) else []
    if any(
        isinstance(row, dict)
        and str(row.get("character_id") or "") != pov_id
        and _norm(row.get("action")) == "enter"
        for row in presence_rows
    ):
        refs.add("world:emergent")
        kinds.update({"external_event", "npc_action"})

    if _new_remote_contact(state, extracted, pov_id):
        refs.add("world:emergent")
        kinds.update({"external_event", "npc_action"})

    return {"kinds": kinds, "refs": refs}


def _main_scene_text(scene_output: str) -> str:
    scene = str(scene_output or "")
    for marker in ("\nЧто я могу сделать", "\nЧто я могу сказать", "\nЧто я могу подумать", "\nСостояние:", "\nОтношения:"):
        if marker in scene:
            scene = scene.split(marker, 1)[0]
    return scene.strip()


def _final_segment(scene_output: str) -> str:
    scene = _main_scene_text(scene_output)
    if not scene:
        return ""
    # Never cut the proof phrase in the middle of a short final beat.
    # Long scenes use a bounded final portion; short scenes are already one beat.
    if len(scene) <= 900:
        return scene
    tail_chars = max(900, int(len(scene) * 0.45))
    start = max(0, len(scene) - tail_chars)
    for marker in ("\n\n", "\n", ". ", "? ", "! "):
        boundary = scene.find(marker, start)
        if boundary >= 0:
            return scene[boundary + len(marker):]
    return scene[start:]


def _ending_supported(kind: str, evidence_kinds: set[str]) -> bool:
    return any(kind in _KIND_TO_ENDINGS.get(progress_kind, set()) for progress_kind in evidence_kinds)


def _error(code: str, message: str, **extra: Any) -> None:
    detail: Dict[str, Any] = {"code": code, "message": message}
    detail.update(extra)
    raise HTTPException(status_code=409, detail=detail)


def validate_scene_progression(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    contract = _packet_contract(root, payload)
    if contract is None:
        return

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    evidence = _collect_evidence(root, payload)
    kinds = set(evidence["kinds"])
    refs = set(evidence["refs"])
    explicit_quiet = contract.get("explicit_uneventful_downtime") is True

    proof = extracted.get("scene_progression")
    if not kinds:
        if explicit_quiet:
            if extracted.get("scene_progressed") is True:
                _error(
                    "SCENE_NO_MEANINGFUL_PROGRESSION",
                    "scene_progressed=true is unsupported: explicit uneventful downtime produced no meaningful progression.",
                )
            return
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "This gameplay turn has no evidence-backed meaningful progression. Time, sleep, routine movement, food, device checks and weather alone do not count.",
            time_skip_requested=contract.get("time_skip_requested") is True,
        )

    if extracted.get("scene_progressed") is not True:
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "Meaningful progression exists but scene_progressed must be true and backed by the progression proof.",
        )

    if not isinstance(proof, dict):
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "scene_progression proof is required for every non-downtime gameplay turn.",
        )

    target = str(proof.get("target") or "").strip()
    kind = str(proof.get("kind") or "").strip()
    action = " ".join(str(proof.get("action") or "").split())
    end_state_change = " ".join(str(proof.get("end_state_change") or "").split())
    ending_kind = str(proof.get("ending_kind") or "").strip()
    ending_evidence = " ".join(str(proof.get("ending_evidence_text") or "").split())

    allowed_targets = {
        str(row.get("target_id"))
        for row in contract.get("eligible_targets", [])
        if isinstance(row, dict) and row.get("target_id")
    }
    if (
        not target
        or target not in allowed_targets
        or kind not in _ALLOWED_KINDS
        or not action
        or not end_state_change
        or ending_kind not in _ALLOWED_ENDINGS
        or not ending_evidence
    ):
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "scene_progression must name one eligible target and provide complete evidence-backed progression/action/end fields.",
            eligible_target_ids=sorted(allowed_targets),
        )

    cast_rows = contract.get("cast_character_ids") if isinstance(contract.get("cast_character_ids"), list) else []
    allowed_cast_refs = {f"cast:{cid}" for cid in cast_rows}
    if target == "cast:independent":
        actor = str(proof.get("character_id") or "").strip()
        if not actor or f"cast:{actor}" not in refs or f"cast:{actor}" not in allowed_cast_refs:
            _error("SCENE_NO_MEANINGFUL_PROGRESSION", "Independent cast proof must name an eligible offscreen NPC who actually acted.", character_id=actor)
    elif target not in refs:
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "The selected progression_target did not actually change in persistence.",
            progression_target=target,
            evidence_refs=sorted(refs),
        )

    allowed_for_target: set[str] = set()
    for prefix, allowed in _TARGET_KIND_PREFIXES.items():
        if target.startswith(prefix):
            allowed_for_target = allowed
            break
    if allowed_for_target and kind not in allowed_for_target:
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "The declared progression kind does not match the selected target type.",
            progression_target=target,
            progression_kind=kind,
            allowed_kinds=sorted(allowed_for_target),
        )
    if kind not in kinds:
        _error(
            "SCENE_NO_MEANINGFUL_PROGRESSION",
            "The declared progression kind is not supported by the persisted changes in this turn.",
            progression_kind=kind,
            evidence_kinds=sorted(kinds),
        )

    final = _norm(_final_segment(str(payload.get("scene_output") or "")))
    ending_evidence_norm = _norm(ending_evidence)
    if (
        not ending_evidence_norm
        or ending_evidence_norm not in final
        or _routine_only(ending_evidence)
        or _PASSIVE_END_RE.search(ending_evidence_norm)
        or not _ending_supported(ending_kind, kinds)
    ):
        _error(
            "SCENE_PASSIVE_ENDING",
            "The final portion is not backed by a concrete progression consequence. Rewrite the final beat around an actual persisted event/state change, not routine completion or an ominous sentence.",
            ending_kind=ending_kind,
            evidence_kinds=sorted(kinds),
        )


def strip_progression_proof(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if isinstance(extracted, dict):
        extracted.pop("scene_progression", None)
    return result
