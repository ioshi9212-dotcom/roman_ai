from __future__ import annotations

import json
from typing import Any, Dict

from . import storage
from .character_registry import build_character_registry, normalize_name, refresh_pov_familiarity, registry_instruction


def _clear_legacy_handoff(root, meta: Dict[str, Any]) -> Dict[str, Any]:
    changed = False
    if meta.get("handoff_required"):
        meta["handoff_required"] = False
        changed = True
    if changed:
        storage._write_json(root / "meta.json", meta)
    for name in ("handoff_tail.json", "resume_token.json"):
        path = root / name
        if path.exists():
            path.unlink()
    return meta


def _resolve_character_id(cards, value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("character_id") or value.get("id") or value.get("name")
    if not value:
        return None
    needle = normalize_name(value)
    for card in cards:
        cid = storage._card_id(card)
        if normalize_name(cid) == needle:
            return cid
        for alias in storage._card_names(card):
            if normalize_name(alias) == needle:
                return cid
    return None


def _canonicalize_state_character_refs(cards, state: Dict[str, Any]) -> Dict[str, Any]:
    result = storage._deep_merge({}, state if isinstance(state, dict) else {})

    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    resolved = _resolve_character_id(cards, pov.get("character_id")) if isinstance(pov, dict) else None
    if resolved:
        pov["character_id"] = resolved
    result["pov"] = pov

    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    present = current.get("present_characters", [])
    values = list(present.keys()) if isinstance(present, dict) else [present] if isinstance(present, str) else present if isinstance(present, list) else []
    canonical = []
    for value in values:
        resolved = _resolve_character_id(cards, value)
        if resolved:
            canonical.append(resolved)
        elif isinstance(value, dict):
            raw = value.get("character_id") or value.get("id") or value.get("name")
            if raw:
                canonical.append(str(raw))
        elif value:
            canonical.append(str(value))
    current["present_characters"] = list(dict.fromkeys(canonical))
    result["current"] = current
    return result


def _refresh_session_familiarity(session_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    original_state = storage._read_json(root / "state.json", {})
    state = _canonicalize_state_character_refs(cards, original_state)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    chronology = storage._read_json(root / "chronology.json", [])
    turns = storage._read_turns(root)
    meta = storage._read_json(root / "meta.json", {})
    refreshed = refresh_pov_familiarity(
        cards,
        state,
        memory,
        chronology,
        turns,
        int(meta.get("turn_number", 0)),
    )
    if refreshed != original_state:
        storage._write_json(root / "state.json", refreshed)
    return {
        "root": root,
        "source": source,
        "cards": cards,
        "state": refreshed,
        "memory": memory,
        "chronology": chronology,
        "turns": turns,
        "meta": meta,
    }


def _augment_packet(session_id: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    raw = "".join(packet.get("chunks", []))
    if not raw:
        return manifest
    context = json.loads(raw)

    snapshot = _refresh_session_familiarity(session_id)
    registry = build_character_registry(snapshot["cards"], snapshot["state"])
    by_id = {row["character_id"]: row for row in registry if row.get("character_id")}

    context["scene_state"] = snapshot["state"]
    context["character_registry"] = registry
    context["character_registry_instruction"] = registry_instruction()

    cast_index = context.get("cast_index", [])
    if isinstance(cast_index, list):
        for row in cast_index:
            if not isinstance(row, dict):
                continue
            registry_row = by_id.get(str(row.get("character_id") or ""))
            if registry_row:
                row["role"] = registry_row.get("role") or row.get("role")
                row["pov_familiarity"] = registry_row.get("pov_familiarity")

    scene_characters = context.get("scene_characters", {})
    if isinstance(scene_characters, dict):
        for cid, bundle in scene_characters.items():
            if not isinstance(bundle, dict):
                continue
            registry_row = by_id.get(str(cid))
            if registry_row:
                bundle["pov_familiarity"] = registry_row.get("pov_familiarity")
                bundle["continuity_rule"] = (
                    "Check pov_familiarity before recognition or introduction. known/acquainted forbids a first-time introduction; "
                    "encountered means prior co-presence but identity may still be unknown."
                )

    context.setdefault("knowledge_boundary", {})["identity_continuity"] = (
        "Who knows a person's identity is separate from objective card truth. Use character_registry.pov_familiarity plus POV personal_memory. "
        "Do not re-introduce known/acquainted characters. Do not name an encountered-but-unknown person through POV until identity is learned."
    )

    author_context = context.get("author_context") if isinstance(context.get("author_context"), dict) else {}
    author_context["registered_character_names"] = [
        {"character_id": row.get("character_id"), "name": row.get("name"), "role": row.get("role")}
        for row in registry
    ]
    context["author_context"] = author_context

    # Keep the original compact packet fields available at top level too. Older
    # scenes/tests and some Custom GPT instructions still read these names directly.
    for key in ("novel", "novel_rules", "novel_lore", "hidden_lore", "story_direction", "world_canon", "character_cards", "relationships", "chronology_recent", "recent_turns"):
        if key in author_context:
            context[key] = author_context[key]

    text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    chunks = [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]
    packet["chunks"] = chunks
    packet["chunk_count"] = len(chunks)
    packet["read_chunks"] = []
    storage._write_json(root / "turn_packet.json", packet)

    result = dict(manifest)
    result["chunk_count"] = len(chunks)
    result["character_registry_count"] = len(registry)
    result["instruction"] = (
        "Read every chunk. Check character_registry before introducing, naming or recognizing anyone. "
        "If an existing offscreen registered character enters, call getCharacterBundle first."
    )
    return result


def prepare_turn_packet(session_id: str, user_input: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    manifest = storage.prepare_turn_packet(session_id, user_input)
    return _augment_packet(session_id, manifest)


def commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    result = storage.commit_turn(session_id, payload)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    result = dict(result)
    result["handoff_required"] = False
    return result


def commit_audit(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    result = storage.commit_audit(session_id, payload)
    meta = storage._read_json(root / "meta.json", {})
    _clear_legacy_handoff(root, meta)
    _refresh_session_familiarity(session_id)
    result = dict(result)
    result["handoff_required"] = False
    return result


def continue_session(session_id: str) -> Dict[str, Any]:
    snapshot = _refresh_session_familiarity(session_id)
    root = snapshot["root"]
    meta = _clear_legacy_handoff(root, snapshot["meta"])
    state = snapshot["state"]
    current = state.get("current", {}) if isinstance(state.get("current"), dict) else {}
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    if isinstance(present, str):
        present = [present]
    return {
        "ok": True,
        "session_id": session_id,
        "turn_number": int(meta.get("turn_number", 0)),
        "last_audit_turn": int(meta.get("last_audit_turn", 0) or 0),
        "audit_required": bool(meta.get("audit_required")),
        "current": {
            "date": current.get("date") or current.get("game_date") or current.get("calendar_date"),
            "time": current.get("time") or current.get("game_time"),
            "location": current.get("location") or current.get("place") or current.get("area"),
            "scene": current.get("scene") or current.get("scene_name") or current.get("situation"),
            "present_character_ids": present if isinstance(present, list) else [],
        },
        "character_registry": build_character_registry(snapshot["cards"], state),
        "instruction": (
            "Continue this exact existing session. Nothing was copied, transferred or recreated. "
            "On the next gameplay input call prepareTurn for this same session_id; that packet will load recent turns, current state, "
            "personal memories, character registry and relevant full cards directly from persistent session storage."
        ),
    }
