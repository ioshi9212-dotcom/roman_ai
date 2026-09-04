from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from . import audit_runtime, session_recovery, session_runtime, storage
from .game_day import sync_game_day
from .transactional_storage import json_text, recover, session_transaction, write_batch


_ORIGINAL_PREPARE_TURN = None
_ORIGINAL_CONTINUE_SESSION = None
_ORIGINAL_RECOVER_CURRENT = None

_HEADER_RE = re.compile(
    r"🕒\s*День\s+(?P<day>\d+)\s*·[^\n]*?(?P<date>\d{2}\.\d{2}\.\d{4})\s*,\s*(?P<time>\d{1,2}:\d{2})\s*·\s*📍\s*(?P<location>[^\n]+)",
    re.IGNORECASE,
)


def _scene_header_current(scene_output: str) -> Dict[str, Any]:
    if not isinstance(scene_output, str):
        return {}
    match = _HEADER_RE.search(scene_output[:1200])
    if not match:
        return {}
    result: Dict[str, Any] = {
        "date": match.group("date").strip(),
        "time": match.group("time").strip(),
        "location": match.group("location").strip(),
    }
    try:
        result["game_day"] = max(1, int(match.group("day")))
    except (TypeError, ValueError):
        pass
    return result


def _turns_text(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return ""
    return "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns)


def _atomic_commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        if meta.get("audit_required"):
            raise RuntimeError("AUDIT_REQUIRED")
        turn_number = int(meta.get("turn_number", 0)) + 1
        packet = storage._read_json(root / "turn_packet.json", {})
        if not packet or packet.get("prepared_for_turn") != turn_number or packet.get("user_input") != payload.get("user_input"):
            raise RuntimeError("TURN_PACKET_REQUIRED")
        if len(set(packet.get("read_chunks", []))) < int(packet.get("chunk_count", 0)):
            raise RuntimeError("TURN_PACKET_INCOMPLETE")

        extracted = payload.get("extracted", {}) if isinstance(payload.get("extracted"), dict) else {}
        entry = storage._template("turn.json", {})
        entry.update(
            {
                "turn_number": turn_number,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "user_input": payload["user_input"],
                "scene_output": payload["scene_output"],
                "extracted": deepcopy(extracted),
            }
        )

        source = storage._read_json(root / "source.json", {})
        cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)

        state = storage._read_json(root / "state.json", {})
        if isinstance(extracted.get("state_patch"), dict):
            state = storage._deep_merge(state, extracted["state_patch"])
        header_current = _scene_header_current(payload.get("scene_output", ""))
        if header_current:
            current = state.get("current") if isinstance(state.get("current"), dict) else {}
            current = storage._deep_merge(current, header_current)
            state["current"] = current
        state = sync_game_day(state, source)
        state = storage._refresh_runtime_presence(state, cards, turn_number)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        for card in cards:
            storage._memory_bucket(memory, storage._card_id(card))
        memory = storage._apply_memory_events(memory, extracted, turn_number)

        chronology = storage._read_json(root / "chronology.json", [])
        if not isinstance(chronology, list):
            chronology = []
        if isinstance(extracted.get("chronology"), list):
            chronology = [*chronology, *deepcopy(extracted["chronology"])]

        turns = storage._read_turns(root)
        turns.append(entry)

        meta["turn_number"] = turn_number
        audit_due = turn_number % 15 == 0
        meta["audit_required"] = bool(audit_due)
        meta["handoff_required"] = False

        write_batch(
            root,
            {
                "turns.jsonl": _turns_text(turns),
                "characters.json": json_text(cards),
                "state.json": json_text(state),
                "memory.json": json_text(memory),
                "chronology.json": json_text(chronology),
                "meta.json": json_text(meta),
            },
        )
        packet_path = root / "turn_packet.json"
        packet_path.unlink(missing_ok=True)

        return {
            "ok": True,
            "turn_number": turn_number,
            "audit_due": audit_due,
            "audit_range": [max(1, turn_number - 14), turn_number] if audit_due else None,
            "handoff_required": False,
            "transactional_commit": True,
        }


def _atomic_commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        expected_end = int(meta.get("turn_number", 0))
        if not meta.get("audit_required"):
            raise RuntimeError("AUDIT_NOT_REQUIRED")
        if int(payload.get("end_turn", 0)) != expected_end:
            raise ValueError("AUDIT_RANGE_MISMATCH")

        repairs = payload.get("repairs", {}) if isinstance(payload.get("repairs"), dict) else {}
        state = storage._read_json(root / "state.json", {})
        if isinstance(repairs.get("state_patch"), dict):
            state = storage._deep_merge(state, repairs["state_patch"])
        source = storage._read_json(root / "source.json", {})
        state = sync_game_day(state, source)

        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        memory = storage._apply_memory_events(memory, repairs, expected_end)

        chronology = storage._read_json(root / "chronology.json", [])
        if not isinstance(chronology, list):
            chronology = []
        if isinstance(repairs.get("chronology_add"), list):
            chronology = [*chronology, *deepcopy(repairs["chronology_add"])]

        audits = storage._read_json(root / "audits.json", [])
        if not isinstance(audits, list):
            audits = []
        audits.append(
            {
                "start_turn": payload["start_turn"],
                "end_turn": payload["end_turn"],
                "repairs": deepcopy(repairs),
                "notes": deepcopy(payload.get("notes", [])),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        meta["last_audit_turn"] = payload["end_turn"]
        meta["audit_required"] = False
        meta["handoff_required"] = False

        write_batch(
            root,
            {
                "state.json": json_text(state),
                "memory.json": json_text(memory),
                "chronology.json": json_text(chronology),
                "audits.json": json_text(audits),
                "meta.json": json_text(meta),
            },
        )
        return {
            "ok": True,
            "audited_through": payload["end_turn"],
            "handoff_required": False,
            "transactional_commit": True,
        }


def _recover_session(session_id: str) -> None:
    root = storage.SESSIONS_DIR / session_id
    if root.exists():
        with session_transaction(root):
            recover(root)


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    _recover_session(session_id)
    result = dict(_ORIGINAL_PREPARE_TURN(session_id, user_input))
    result.pop("full_character_card_count", None)
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    try:
        context = json.loads("".join(packet.get("chunks", [])))
        result["scene_character_card_count"] = len(context.get("scene_character_cards", []))
        result["working_context"] = True
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    result["instruction"] = (
        "Read every packet chunk before writing. scene_builder and runtime rules are mandatory. "
        "The packet contains full source/current state plus complete cards and personal memory for the scene-relevant cast. "
        "If another registered character enters, load getCharacterBundle before writing that character. Persist every durable new fact before commit."
    )
    return result


def _continue(session_id: str) -> Dict[str, Any]:
    _recover_session(session_id)
    return _ORIGINAL_CONTINUE_SESSION(session_id)


def _recover_current(session_id: str) -> Dict[str, Any]:
    _recover_session(session_id)
    return _ORIGINAL_RECOVER_CURRENT(session_id)


def install() -> None:
    global _ORIGINAL_PREPARE_TURN, _ORIGINAL_CONTINUE_SESSION, _ORIGINAL_RECOVER_CURRENT
    _ORIGINAL_PREPARE_TURN = session_runtime.prepare_turn_packet
    _ORIGINAL_CONTINUE_SESSION = session_runtime.continue_session
    _ORIGINAL_RECOVER_CURRENT = session_recovery.recover_session_current

    storage.commit_turn = _atomic_commit_turn
    storage.commit_audit = _atomic_commit_audit
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.continue_session = _continue
    session_recovery.recover_session_current = _recover_current
