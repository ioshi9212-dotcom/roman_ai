from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from . import session_recovery, storage
from .session_start_guard import normalise_session_start, session_start_status


_ORIGINAL_RECOVER_SESSION_CURRENT = session_recovery.recover_session_current
_INSTALLED = False


def recover_session_current(session_id: str) -> Dict[str, Any]:
    try:
        return _ORIGINAL_RECOVER_SESSION_CURRENT(session_id)
    except RuntimeError as exc:
        if str(exc) != "CURRENT_RECOVERY_NO_EVIDENCE":
            raise

    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0)
    if turn_number != 0:
        raise RuntimeError("CURRENT_RECOVERY_NO_EVIDENCE")

    source = storage._read_json(root / "source.json", {})
    prepared = normalise_session_start(source)
    start_status = session_start_status(prepared)
    if not start_status["ready"]:
        raise RuntimeError("CURRENT_RECOVERY_NO_EVIDENCE")

    prepared_state = (
        prepared.get("starting_state")
        if isinstance(prepared.get("starting_state"), dict)
        else {}
    )
    recovered_current = (
        deepcopy(prepared_state.get("current"))
        if isinstance(prepared_state.get("current"), dict)
        else {}
    )
    recovered_pov = (
        deepcopy(prepared_state.get("pov"))
        if isinstance(prepared_state.get("pov"), dict)
        else {}
    )

    state = storage._read_json(root / "state.json", {})
    state = deepcopy(state if isinstance(state, dict) else {})
    state["current"] = recovered_current
    if recovered_pov.get("character_id"):
        current_pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
        current_pov = deepcopy(current_pov)
        current_pov["character_id"] = recovered_pov["character_id"]
        state["pov"] = current_pov

    cards = storage._load_cards(root, source)
    state = storage._refresh_runtime_presence(state, cards, 0)
    storage._write_json(root / "state.json", state)

    for name in ("turn_packet.json", "audit_packet.json"):
        path = root / name
        if path.exists():
            path.unlink()

    before = session_recovery.current_recovery_status(session_id)
    if before["required"]:
        raise RuntimeError("CURRENT_RECOVERY_NO_EVIDENCE")

    meta["last_current_recovery"] = {
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "turn_number": 0,
        "reason": ["turn_zero_start_shape_invalid"],
        "provenance": {
            "turn_zero_intake_guard": True,
            "source_starting_state_normalized_in_memory": True,
            "source_canon_rewritten": False,
        },
    }
    storage._write_json(root / "meta.json", meta)

    return {
        "ok": True,
        "session_id": session_id,
        "changed": True,
        "turn_number": 0,
        "current": deepcopy(state["current"]),
        "provenance": deepcopy(meta["last_current_recovery"]["provenance"]),
        "canon_mutated": False,
        "turn_created": False,
        "instruction": (
            "Turn-zero current pointer was reconstructed from the saved starting setup using "
            "the tolerant intake normalizer. Call resumeSession again, then continue normally."
        ),
    }


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    session_recovery.recover_session_current = recover_session_current
    _INSTALLED = True
