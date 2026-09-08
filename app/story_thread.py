from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable


_TERMINAL = {"resolved", "closed", "expired", "cancelled", "canceled", "done", "abandoned"}


def _as_dict_threads(value: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(value, dict):
        return {str(key): deepcopy(item) for key, item in value.items() if isinstance(item, dict)}
    if isinstance(value, list):
        result: Dict[str, Dict[str, Any]] = {}
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            thread_id = str(item.get("thread_id") or item.get("id") or index)
            result[thread_id] = deepcopy(item)
        return result
    return {}


def _merge_unique(existing: Any, incoming: Any) -> list[Any]:
    values: list[Any] = []
    seen: set[str] = set()
    for source in (existing, incoming):
        for item in source if isinstance(source, list) else []:
            marker = repr(item)
            if marker in seen:
                continue
            seen.add(marker)
            values.append(deepcopy(item))
    return values


def _operation(raw: Dict[str, Any]) -> str:
    return str(raw.get("operation") or "upsert").casefold().strip()


def _progressed(raw: Dict[str, Any], *, is_new: bool) -> bool:
    if raw.get("progressed_now") is True:
        return True
    if raw.get("progressed_now") is False:
        return False
    return is_new or _operation(raw) in {"resolve", "abandon"}


def apply_updates(state: Dict[str, Any], updates: Iterable[Dict[str, Any]], *, current_turn: int) -> Dict[str, Any]:
    result = deepcopy(state) if isinstance(state, dict) else {}
    threads = _as_dict_threads(result.get("threads"))

    for raw in updates:
        if not isinstance(raw, dict):
            continue
        thread_id = str(raw.get("thread_id") or raw.get("id") or "").strip()
        if not thread_id:
            continue
        operation = _operation(raw)
        existing = deepcopy(threads.get(thread_id, {}))
        is_new = not bool(existing)

        if operation in {"resolve", "abandon"}:
            if not existing:
                continue
            existing["status"] = "resolved" if operation == "resolve" else "abandoned"
            existing["resolved_turn"] = current_turn
            existing["last_progress_turn"] = current_turn
            existing["progress_count"] = int(existing.get("progress_count", 0) or 0) + 1
            resolution = raw.get("resolution") or raw.get("progress_summary") or raw.get("summary")
            if resolution not in (None, ""):
                existing["resolution"] = str(resolution)
                existing["last_progress_summary"] = str(resolution)
            threads[thread_id] = existing
            continue

        item = existing
        item["thread_id"] = thread_id
        item.setdefault("status", "active")
        item.setdefault("created_turn", current_turn)

        scalar_fields = (
            "title", "summary", "priority", "premise", "current_goal", "current_phase",
            "progress_summary", "resolution", "next_eligible_game_day",
        )
        for key in scalar_fields:
            if key in raw and raw.get(key) not in (None, ""):
                item[key] = deepcopy(raw[key])

        for key in ("unresolved", "end_conditions", "possible_routes"):
            if key in raw and isinstance(raw.get(key), list):
                item[key] = deepcopy(raw[key])

        if "participants" in raw and isinstance(raw.get("participants"), list):
            item["participants"] = _merge_unique(item.get("participants"), raw.get("participants"))

        if "anchor_facts" in raw and isinstance(raw.get("anchor_facts"), list):
            item["anchor_facts"] = _merge_unique(item.get("anchor_facts"), raw.get("anchor_facts"))

        if str(item.get("status") or "").casefold() in _TERMINAL and raw.get("status") not in (None, ""):
            item["status"] = raw["status"]

        if _progressed(raw, is_new=is_new):
            item["last_progress_turn"] = current_turn
            item["progress_count"] = int(item.get("progress_count", 0) or 0) + 1
            progress_summary = raw.get("progress_summary") or raw.get("summary")
            if progress_summary not in (None, ""):
                item["last_progress_summary"] = str(progress_summary)
        elif is_new:
            item.setdefault("last_progress_turn", current_turn)

        threads[thread_id] = item

    result["threads"] = threads
    return result


def active_threads(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    threads = _as_dict_threads(state.get("threads") if isinstance(state, dict) else {})
    return {
        thread_id: item
        for thread_id, item in threads.items()
        if str(item.get("status") or "active").casefold().strip() not in _TERMINAL
    }
