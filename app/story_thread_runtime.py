from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, Iterable

from fastapi import HTTPException

from . import session_runtime, storage, writer_first_runtime
from .story_thread import apply_updates, active_threads
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT_TURN = None
_ORIGINAL_COMMIT_AUDIT = None
_STORY_ENGINE_VERSION = 7
_STAGNATION_LIMIT = 3
_THREAD_SOFT_AGE = 4
_THREAD_HARD_AGE = 6
_RELATIONSHIP_DELTA_RE = re.compile(r"/delta\s*[+-]?\d", re.IGNORECASE)
_TERMINAL = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}


def _updates_progress(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for raw in values:
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "upsert").casefold().strip()
        if operation in {"resolve", "abandon"}:
            return True
        if raw.get("progressed_now") is True:
            return True
        if operation == "upsert" and raw.get("progressed_now") is not False:
            return True
    return False


def _intent_progress(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for raw in values:
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "upsert").casefold().strip()
        if raw.get("pursued_now") is True or operation in {"resolve", "abandon"}:
            return True
        if operation == "upsert" and (raw.get("summary") or raw.get("planned_action")):
            return True
    return False


def _container_has_progress(container: Any, scene_output: str = "") -> bool:
    if not isinstance(container, dict):
        return False
    if container.get("scene_progressed") is True:
        return True
    if isinstance(container.get("chronology"), list) and container["chronology"]:
        return True
    if _updates_progress(container.get("story_thread_updates")):
        return True
    if _intent_progress(container.get("npc_intent_updates")):
        return True
    for key in ("presence_updates", "relationship_updates", "character_upserts"):
        if isinstance(container.get(key), list) and container[key]:
            return True
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    world_patch = patch.get("world") if isinstance(patch.get("world"), dict) else {}
    meaningful_world_patch = {key: value for key, value in world_patch.items() if key != "cast_registry"}
    if meaningful_world_patch:
        return True
    return bool(_RELATIONSHIP_DELTA_RE.search(str(scene_output or "")))


def _turn_has_progress(turn: Dict[str, Any]) -> bool:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    return _container_has_progress(extracted, str(turn.get("scene_output") or ""))


def trailing_stagnant_turns(root, *, limit: int = 12) -> int:
    turns = storage._read_turns(root)
    count = 0
    for turn in reversed(turns[-limit:]):
        if not isinstance(turn, dict) or _turn_has_progress(turn):
            break
        count += 1
    return count


def _priority(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    raw = value.get("priority")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    return {"critical": 100, "high": 75, "medium": 50, "normal": 50, "low": 25}.get(str(raw or "").casefold(), 0)


def _thread_turn(thread: Dict[str, Any]) -> int | None:
    for key in ("last_progress_turn", "last_turn", "updated_turn", "last_updated_turn", "start_turn", "created_turn"):
        raw = thread.get(key)
        try:
            if raw not in (None, ""):
                return int(raw)
        except (TypeError, ValueError):
            pass
    return None


def _story_pressure(context: Dict[str, Any], current_turn: int) -> list[Dict[str, Any]]:
    value = context.get("active_threads")
    if isinstance(value, dict):
        rows = [(str(key), item) for key, item in value.items() if isinstance(item, dict)]
    elif isinstance(value, list):
        rows = [
            (str(item.get("thread_id") or item.get("id") or index), item)
            for index, item in enumerate(value) if isinstance(item, dict)
        ]
    else:
        rows = []

    result: list[tuple[int, Dict[str, Any]]] = []
    for thread_id, thread in rows:
        if str(thread.get("status") or "active").casefold().strip() in _TERMINAL:
            continue
        priority = _priority(thread)
        last_turn = _thread_turn(thread)
        age = max(0, current_turn - last_turn) if last_turn is not None else current_turn
        soft_due = age >= _THREAD_SOFT_AGE or priority >= 75
        if not soft_due:
            continue
        hard_due = age >= _THREAD_HARD_AGE or priority >= 100
        result.append((priority + age, {
            "thread_id": thread_id,
            "summary": thread.get("summary") or thread.get("title") or thread.get("premise"),
            "priority": priority,
            "turns_since_progress": age,
            "must_advance_or_causally_pause": hard_due,
            "current_goal": thread.get("current_goal"),
            "current_phase": thread.get("current_phase"),
            "unresolved": deepcopy(thread.get("unresolved")) if isinstance(thread.get("unresolved"), list) else None,
            "guidance": "Продвинь причинно; если сейчас нельзя — сохрани понятную паузу/следующее условие.",
        }))
    result.sort(key=lambda pair: pair[0], reverse=True)
    return [{key: value for key, value in item.items() if value not in (None, "", [], False)} for _, item in result[:6]]


def _flatten_future(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    if depth > 3:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _flatten_future(child, child_path, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:8]):
            yield from _flatten_future(child, f"{path}[{index}]", depth + 1)
    elif isinstance(value, (str, int, float, bool)):
        text = " ".join(str(value).split())
        if text:
            yield path, text[:260]


def _future_direction_cues(context: Dict[str, Any]) -> list[Dict[str, str]]:
    future = context.get("future_guidance") if isinstance(context.get("future_guidance"), dict) else {}
    direction = future.get("story_direction")
    result: list[Dict[str, str]] = []
    seen: set[str] = set()
    for path, text in _flatten_future(direction):
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append({"path": path, "guidance": text})
        if len(result) >= 6:
            break
    return result


def _story_drive(context: Dict[str, Any], root, current_turn: int, pressure: list[Dict[str, Any]]) -> Dict[str, Any]:
    streak = trailing_stagnant_turns(root)
    hard_thread_due = any(item.get("must_advance_or_causally_pause") is True for item in pressure)
    return {
        "mandatory": True,
        "stagnant_turns": streak,
        "force_progress_this_turn": streak >= _STAGNATION_LIMIT or hard_thread_due,
        "active_thread_count": len(active_threads(storage._read_json(root / "state.json", {}))),
        "future_direction_cues": _future_direction_cues(context),
        "scene_progress_flag": "scene_progressed=true только при реальном изменении ситуации; перемещение, ожидание и течение времени сами по себе не прогресс.",
        "rule": "Двигай существующие линии причинно. Значимый выбор POV оставляй игроку. Просроченную линию продвинь или явно поставь на причинную паузу.",
    }


def _progress_required_error(streak: int) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "STORY_PROGRESS_REQUIRED",
            "message": "Static streak: continue/compress to a real change, interruption, natural end or meaningful POV choice. Routine/position/time alone are not progress.",
            "stagnant_turns_before_this_commit": streak,
            "maximum_consecutive_static_turns": _STAGNATION_LIMIT,
        },
    )


def _with_story_patch(session_id: str, payload: Dict[str, Any], *, audit: bool = False) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0) + (0 if audit else 1)

    container_key = "repairs" if audit else "extracted"
    container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
    updates = container.get("story_thread_updates")

    if not audit and isinstance(updates, list):
        streak = trailing_stagnant_turns(root)
        if streak >= _STAGNATION_LIMIT and not _container_has_progress(container, str(result.get("scene_output") or "")):
            _progress_required_error(streak)

    if not isinstance(updates, list) or not updates:
        return result

    updated_state = apply_updates(state, updates, current_turn=current_turn)
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    patch = deepcopy(patch)
    patch["threads"] = deepcopy(updated_state.get("threads", {}))
    container = deepcopy(container)
    container["state_patch"] = patch
    result[container_key] = container
    return result


def _clean_relationship_policy(context: Dict[str, Any]) -> None:
    policy = context.get("relationship_policy")
    if not isinstance(policy, dict):
        return
    policy = deepcopy(policy)
    source = str(policy.get("source_of_truth") or "")
    if not source or "relationship_contract" in source:
        policy["source_of_truth"] = "relationship_lens"
    for key in ("instruction", "authoritative_start_snapshot_note"):
        value = policy.get(key)
        if isinstance(value, str):
            value = value.replace("relationship_lens + relationship_contract", "relationship_lens")
            value = value.replace("relationship_lens and relationship_contract", "relationship_lens")
            policy[key] = value
    context["relationship_policy"] = policy


def _rewrite_story_drive(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base
        if packet.get("story_engine_version") == _STORY_ENGINE_VERSION:
            return base
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base

        current_turn = max(0, int(packet.get("prepared_for_turn", 1) or 1) - 1)
        guardrails = context.get("narrative_guardrails") if isinstance(context.get("narrative_guardrails"), dict) else {}
        pressure = _story_pressure(context, current_turn)
        guardrails["story_pressure"] = pressure
        guardrails["story_drive"] = _story_drive(context, root, current_turn, pressure)
        prior_instruction = str(guardrails.get("instruction") or "").strip()
        guardrails["instruction"] = (
            "Обязательные правила соблюдай. Pressure — напоминания: учитывай причинно, без выдуманного прошлого."
        )
        context["narrative_guardrails"] = guardrails
        _clean_relationship_policy(context)

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["story_engine_version"] = _STORY_ENGINE_VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "story_engine": True,
            "story_engine_version": _STORY_ENGINE_VERSION,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_story_drive(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_TURN(session_id, _with_story_patch(session_id, payload, audit=False))


def _commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_AUDIT(session_id, _with_story_patch(session_id, payload, audit=True))


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT_TURN, _ORIGINAL_COMMIT_AUDIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT_TURN = session_runtime.commit_turn
    _ORIGINAL_COMMIT_AUDIT = session_runtime.commit_audit
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
    session_runtime.commit_audit = _commit_audit
