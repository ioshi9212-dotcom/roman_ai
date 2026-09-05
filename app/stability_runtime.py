from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict

from . import session_recovery, session_runtime, storage
from .game_day import sync_game_day
from .relationship_runtime import overwrite_relationship_snapshots
from .transactional_storage import json_text, recover, session_transaction, write_batch


_ORIGINAL_PREPARE_TURN = None
_ORIGINAL_CONTINUE_SESSION = None
_ORIGINAL_RECOVER_CURRENT = None

_HEADER_RE = re.compile(
    r"🕒\s*День\s+(?P<day>\d+)\s*·[^\n]*?(?P<date>\d{2}\.\d{2}\.\d{4})\s*,\s*(?P<time>\d{1,2}:\d{2})\s*·\s*📍\s*(?P<location>[^\n]+)",
    re.IGNORECASE,
)

_SOURCE_STANDARD_KEYS = {
    "novel_id",
    "title",
    "version",
    "novel",
    "characters",
    "lore",
    "rules",
    "hidden_lore",
    "world",
    "starting_state",
    "story_direction",
}


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
    return "".join(json.dumps(turn, ensure_ascii=False) + "\n" for turn in turns)


def _clean_scene_pointer(state: Dict[str, Any], extracted: Dict[str, Any]) -> Dict[str, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    state["current"] = current
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    current_patch = state_patch.get("current") if isinstance(state_patch.get("current"), dict) else {}

    if "entered_characters" not in current_patch:
        current.pop("entered_characters", None)
    if "left_characters" not in current_patch:
        current.pop("left_characters", None)

    present = set(str(value) for value in storage._present_character_ids(state) if value)
    positions = current.get("positions") if isinstance(current.get("positions"), dict) else None
    if positions is not None:
        current["positions"] = {
            str(character_id): deepcopy(position)
            for character_id, position in positions.items()
            if str(character_id) in present
        }
    return state


def _merge_state_patch_exact_relationships(state: Dict[str, Any], patch: Any) -> Dict[str, Any]:
    if not isinstance(patch, dict):
        return state
    result = storage._deep_merge(state, patch)
    relationship_patch = {
        key: deepcopy(patch[key])
        for key in ("relationships", "relationship_documents")
        if isinstance(patch.get(key), dict)
    }
    if relationship_patch:
        result = overwrite_relationship_snapshots(result, relationship_patch)
    return result


def _compact_turn_context(context: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Keep one canonical copy of each working-context block after all compatibility wrappers ran."""
    for obsolete in (
        "source_full",
        "state_full",
        "scene_character_ids",
        "scene_character_cards",
        "scene_character_memory",
        "character_registry_index",
        "source_character_cards_omitted_from_transport",
    ):
        context.pop(obsolete, None)

    context["starting_state"] = deepcopy(source.get("starting_state", {}))
    context["source_meta"] = {
        key: deepcopy(source[key])
        for key in ("novel_id", "title", "version")
        if key in source
    }
    extras = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in _SOURCE_STANDARD_KEYS
    }
    if extras:
        context["source_extra"] = extras
    else:
        context.pop("source_extra", None)

    author = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    for duplicate in (
        "novel",
        "novel_rules",
        "novel_lore",
        "hidden_lore",
        "story_direction",
        "world_canon",
        "character_cards",
        "relationships",
        "chronology_recent",
        "recent_turns",
        "registered_character_names",
    ):
        author.pop(duplicate, None)
    author["instruction"] = (
        "Auxiliary author-only metadata. Canonical working data lives at the top-level scene_builder paths: "
        "scene_state, character_cards, character_memory, character_registry, novel/novel_rules/novel_lore/hidden_lore/world_canon/story_direction, chronology_recent and recent_turns."
    )
    context["author_context"] = author
    context["transport_context_paths"] = {
        "state": "scene_state",
        "cards": "character_cards",
        "memory": "character_memory",
        "registry": "character_registry",
        "chronology": "chronology_recent",
        "starting_state": "starting_state",
    }
    contract = context.get("working_context_contract") if isinstance(context.get("working_context_contract"), dict) else {}
    contract.update(
        {
            "persistent_storage_is_complete": True,
            "turn_packet_is_scene_scoped": True,
            "single_copy_transport": True,
            "scene_builder_paths_are_canonical": True,
        }
    )
    context["working_context_contract"] = contract
    return context


def _atomic_commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        if meta.get("audit_required"):
            raise RuntimeError("AUDIT_REQUIRED")
        if meta.get("handoff_required"):
            raise RuntimeError("HANDOFF_REQUIRED")
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
        state = _merge_state_patch_exact_relationships(state, extracted.get("state_patch"))
        header_current = _scene_header_current(payload.get("scene_output", ""))
        if header_current:
            current = state.get("current") if isinstance(state.get("current"), dict) else {}
            state["current"] = storage._deep_merge(current, header_current)
        state = _clean_scene_pointer(state, extracted)
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
        handoff_due = turn_number % 60 == 0
        meta["audit_required"] = bool(audit_due)
        if handoff_due:
            meta["handoff_required"] = True

        values = {
            "turns.jsonl": _turns_text(turns),
            "characters.json": json_text(cards),
            "state.json": json_text(state),
            "memory.json": json_text(memory),
            "chronology.json": json_text(chronology),
            "meta.json": json_text(meta),
        }
        if handoff_due:
            values["handoff_tail.json"] = json_text(turns[-6:])
        write_batch(root, values)
        (root / "turn_packet.json").unlink(missing_ok=True)

        return {
            "ok": True,
            "turn_number": turn_number,
            "audit_due": audit_due,
            "audit_range": [max(1, turn_number - 14), turn_number] if audit_due else None,
            "handoff_required": handoff_due,
            "transactional_commit": True,
            "relationship_snapshots_atomic": True,
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
        state = _merge_state_patch_exact_relationships(state, repairs.get("state_patch"))
        source = storage._read_json(root / "source.json", {})
        state = _clean_scene_pointer(state, repairs)
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
            "handoff_required": bool(meta.get("handoff_required")),
            "transactional_commit": True,
            "relationship_snapshots_atomic": True,
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
        instruction = str(context.get("relationship_lens_instruction", ""))
        if "do not output an empty relationship block" not in instruction:
            context["relationship_lens_instruction"] = (
                instruction.rstrip()
                + " For every present NPC, do not output an empty relationship block merely because no numeric baseline existed before this scene."
            ).strip()
        scene_presence = context.get("scene_presence") if isinstance(context.get("scene_presence"), dict) else {}
        roster = scene_presence.get("roster") if isinstance(scene_presence.get("roster"), list) else []
        for row in roster:
            if not isinstance(row, dict) or not row.get("character_id"):
                continue
            character_id = str(row["character_id"])
            row["full_card_path"] = f"character_cards[character_id={character_id}]"
            row["memory_path"] = f"character_memory[{character_id}]"

        source = storage._read_json(root / "source.json", {})
        context = _compact_turn_context(context, source)
        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        chunks = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = []
        packet["stability_context_version"] = 2
        storage._write_json(root / "turn_packet.json", packet)
        result["chunk_count"] = len(chunks)
        result["scene_character_card_count"] = len(context.get("character_cards", []))
        result["working_context"] = True
        result["total_chars"] = len(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    result["instruction"] = (
        "Read every packet chunk before writing. scene_builder and runtime rules are mandatory. "
        "Use the stable scene_builder paths scene_state, character_cards and character_memory. Full persistent storage remains in Railway; "
        "the packet contains the complete current-scene working set without duplicate dormant dossiers. If another registered character enters, "
        "load getCharacterBundle before writing that character. Persist every durable new fact before commit."
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
