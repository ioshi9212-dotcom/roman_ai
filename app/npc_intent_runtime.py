from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import session_runtime, storage
from .npc_intent import apply_updates


_ORIGINAL_COMMIT_TURN = None
_ORIGINAL_COMMIT_AUDIT = None


def _with_intent_patch(session_id: str, payload: Dict[str, Any], *, audit: bool = False) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    current_turn = int(meta.get("turn_number", 0) or 0) + (0 if audit else 1)

    container_key = "repairs" if audit else "extracted"
    container = result.get(container_key) if isinstance(result.get(container_key), dict) else {}
    updates = container.get("npc_intent_updates")
    if not isinstance(updates, list) or not updates:
        return result

    updated_state = apply_updates(state, updates, current_turn=current_turn)
    patch = container.get("state_patch") if isinstance(container.get("state_patch"), dict) else {}
    patch = deepcopy(patch)
    patch["npc_intents"] = deepcopy(updated_state.get("npc_intents", {}))
    container = deepcopy(container)
    container["state_patch"] = patch
    result[container_key] = container
    return result


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_TURN(session_id, _with_intent_patch(session_id, payload, audit=False))


def _commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return _ORIGINAL_COMMIT_AUDIT(session_id, _with_intent_patch(session_id, payload, audit=True))


def install() -> None:
    global _ORIGINAL_COMMIT_TURN, _ORIGINAL_COMMIT_AUDIT
    if _ORIGINAL_COMMIT_TURN is not None:
        return
    _ORIGINAL_COMMIT_TURN = session_runtime.commit_turn
    _ORIGINAL_COMMIT_AUDIT = session_runtime.commit_audit
    session_runtime.commit_turn = _commit_turn
    session_runtime.commit_audit = _commit_audit
