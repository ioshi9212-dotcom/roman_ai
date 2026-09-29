from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from . import storage, turn_context
from .npc_intent import active_intents_for


SELF_HIDDEN_KEYS = {
    "unknown_to_self",
    "hidden_from_self",
    "not_known_to_self",
    "author_only",
}
RAW_WRITER_KEYS = {
    "recent_turns",
    "continuity_turns",
    "scene_history",
    "chronology_recent",
    "chronology_anchor_catalog",
    "character_cards",
    "character_profiles",
    "character_memory",
    "scene_characters",
    "relationship_lens",
    "relationships",
    "relationship_documents",
    "npc_active_intents",
    "author_context",
    "hidden_lore",
    "novel_lore",
    "story_direction",
    "novel",
    "novel_rules",
    "world_canon",
    "cast_index",
    "cast_registry",
    "starting_state",
}
DIRECTOR_NESTED_PRIVATE_KEYS = {
    "hidden_lore",
    "author_only",
    "unknown_to_self",
    "hidden_from_self",
    "not_known_to_self",
    "private_memory",
    "character_memory",
    "chronology",
    "recent_turns",
    "scene_history",
}
SPEECH_RE = re.compile(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*[—-]\s*(?P<text>.*)$")
PRIVATE_REDACTION_MARKER = "содержание приватной коммуникации скрыто"


def _false_flag(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"false", "no", "нет", "0"}
    return False


def _self_hidden_object(value: Dict[str, Any]) -> bool:
    if value.get("author_only") is True:
        return True
    if value.get("unknown_to_self") is True:
        return True
    if value.get("hidden_from_self") is True:
        return True
    if value.get("not_known_to_self") is True:
        return True
    if "known_to_self" in value and _false_flag(value.get("known_to_self")):
        return True
    return False


def self_known_card(value: Any) -> Any:
    """Return the character's own-card knowledge without author-only/self-hidden branches.

    Ordinary biography remains available without requiring duplicate knowledge entries.
    """
    if isinstance(value, dict):
        if _self_hidden_object(value):
            return None
        result: Dict[str, Any] = {}
        for key, item in value.items():
            norm = str(key).strip().casefold()
            if norm in SELF_HIDDEN_KEYS or norm == "known_to_self":
                continue
            cleaned = self_known_card(item)
            if cleaned is not None:
                result[key] = cleaned
        return result
    if isinstance(value, list):
        rows = []
        for item in value:
            cleaned = self_known_card(item)
            if cleaned is not None:
                rows.append(cleaned)
        return rows
    return deepcopy(value)


def _director_safe(value: Any) -> Any:
    """Keep scene-direction structure while removing obvious raw private stores."""
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).strip().casefold() in DIRECTOR_NESTED_PRIVATE_KEYS:
                continue
            result[key] = _director_safe(item)
        return result
    if isinstance(value, list):
        return [_director_safe(item) for item in value]
    return deepcopy(value)


def _novel_direction(source: Dict[str, Any]) -> Dict[str, Any]:
    novel = source.get("novel") if isinstance(source.get("novel"), dict) else {}
    keep = (
        "title",
        "genres",
        "category",
        "setting",
        "premise",
        "tone",
        "world_rules",
        "story_rules",
        "start",
        "supernatural",
        "notes",
        "additional",
    )
    return {
        key: _director_safe(novel[key])
        for key in keep
        if key in novel and novel[key] not in (None, "", [], {})
    }


def _public_dialogue_continuity(context: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    recent = context.get("recent_turns")
    if not isinstance(recent, list):
        return rows
    for turn in recent:
        if not isinstance(turn, dict):
            continue
        scene = str(turn.get("scene_output") or "")
        if not scene:
            continue
        for match in SPEECH_RE.finditer(scene):
            text = match.group("text").strip()
            if not text or PRIVATE_REDACTION_MARKER in text.casefold():
                continue
            rows.append({
                "turn_number": int(turn.get("turn_number", 0) or 0),
                "speaker": match.group("speaker").strip(),
                "text": text,
            })
    return rows[-limit:]


def _scene_character_ids(state: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[str]:
    values = [str(value) for value in storage._present_character_ids(state) if value]
    values.extend(str(value) for value in storage._remote_character_ids(state) if value)
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    if pov.get("character_id"):
        values.insert(0, str(pov["character_id"]))
    valid = {storage._card_id(card) for card in cards if storage._card_id(card)}
    return [cid for cid in dict.fromkeys(values) if cid in valid]


def character_slices(
    *,
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    current_turn: int,
) -> Dict[str, Dict[str, Any]]:
    card_map = {
        storage._card_id(card): card
        for card in cards
        if storage._card_id(card)
    }
    memory_buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    runtime_characters = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    ids = _scene_character_ids(state, cards)
    intents = active_intents_for(state, ids, current_turn=current_turn)
    result: Dict[str, Dict[str, Any]] = {}

    for cid in ids:
        card = card_map.get(cid, {})
        result[cid] = {
            "character_id": cid,
            "self_profile": self_known_card(card) or {},
            "personal_memory": turn_context._working_memory_bucket(
                memory_buckets.get(cid, {}),
                current_turn,
            ),
            "current_state": deepcopy(
                runtime_characters.get(cid, {})
                if isinstance(runtime_characters.get(cid), dict)
                else {}
            ),
            "relationship_to_pov": deepcopy(storage._relationship_hint(state, cid)),
            "active_intents": deepcopy(intents.get(cid, [])),
            "knowledge_scope": {
                "self_profile_is_self_known": True,
                "ordinary_self_facts_do_not_need_duplicate_memory": True,
                "examples_of_ordinary_self_facts": [
                    "age",
                    "name",
                    "appearance",
                    "work",
                    "family",
                    "habits",
                    "remembered biography",
                ],
                "dynamic_personal_facts_source": "personal_memory",
                "current_scene_sources": [
                    "what this character personally sees",
                    "what this character personally hears",
                    "communication actually addressed to this character",
                    "what another character tells this character in the scene",
                    "reasonable inference from already known facts",
                ],
                "not_personal_knowledge": [
                    "director_cues",
                    "objective chronology",
                    "another character's self_profile",
                    "another character's personal_memory",
                    "POV private thoughts",
                    "offscreen private communication not received by this character",
                ],
                "rule": (
                    "Use self_profile freely for this character's own normal biography. "
                    "Do not require an age/job/name/self-history fact to be duplicated in personal_memory. "
                    "For facts about other people or offscreen events, use only this character's personal_memory "
                    "or a source they actually perceive/receive in the current scene."
                ),
            },
        }
    return result


def _current_private_scope(
    context: Dict[str, Any],
    *,
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    # Imported lazily to avoid making private parsing a hard validation dependency.
    from . import private_knowledge_runtime

    user_input = str(context.get("user_input") or "")
    rows = private_knowledge_runtime.extract_private_communications(user_input, cards)
    if not rows:
        return []

    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    active_ids = _scene_character_ids(state, cards)
    result: List[Dict[str, Any]] = []
    for row in rows:
        recipient = str(row.get("recipient_id") or "")
        visible_to = [cid for cid in dict.fromkeys([pov_id, recipient]) if cid]
        result.append({
            "recipient_id": recipient,
            "payload": row.get("payload"),
            "visible_to_character_ids": visible_to,
            "not_visible_to_character_ids": [cid for cid in active_ids if cid not in visible_to],
            "rule": "This communication is not ambient scene knowledge. Only listed recipients receive its content.",
        })
    return result


def apply_sliced_writer_context(
    context: Dict[str, Any],
    *,
    source: Dict[str, Any],
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    current_turn: int,
) -> Dict[str, Any]:
    result = deepcopy(context)

    scene_state = result.get("scene_state") if isinstance(result.get("scene_state"), dict) else {}
    current = scene_state.get("current") if isinstance(scene_state.get("current"), dict) else {}
    if not current:
        current = state.get("current") if isinstance(state.get("current"), dict) else {}

    public_continuity = _public_dialogue_continuity(result)
    private_scope = _current_private_scope(result, cards=cards, state=state)

    director_cues = {
        "novel_direction": _novel_direction(source),
        "novel_rules": _director_safe(result.get("novel_rules", {})),
        "world_canon": _director_safe(result.get("world_canon", {})),
        "active_threads": _director_safe(result.get("active_threads", {})),
        "future_guidance": _director_safe(result.get("future_guidance", {})),
        "cast_registry": _director_safe(result.get("cast_registry", result.get("cast_index", {}))),
        "rule": (
            "Director cues may choose scene movement, consequences and which existing line to advance. "
            "They are NOT a factual source for any NPC. Before an NPC states or acts on a fact about another person "
            "or an offscreen event, that fact must exist in that NPC's character_slice or become available by current perception."
        ),
    }

    scene_contract = {
        "current": deepcopy(current),
        "scene_presence": deepcopy(result.get("scene_presence", {})),
        "player_input_map": deepcopy(result.get("player_input_map", {})),
        "public_dialogue_continuity": public_continuity,
        "current_private_communications": private_scope,
        "continuity_rule": (
            "Use current physical state plus public_dialogue_continuity to continue the scene. "
            "Raw past scenes and raw chronology are intentionally absent from the writer packet. "
            "Missing private/offscreen details must not be reconstructed from director knowledge."
        ),
    }

    slices = character_slices(
        cards=cards,
        state=state,
        memory=memory,
        current_turn=current_turn,
    )

    for key in RAW_WRITER_KEYS:
        result.pop(key, None)
    result.pop("active_threads", None)
    result.pop("future_guidance", None)
    result.pop("scene_state", None)
    result.pop("character_registry", None)
    result.pop("character_registry_instruction", None)

    result["scene_state"] = {"current": deepcopy(current)}
    result["scene_contract"] = scene_contract
    result["director_cues"] = director_cues
    result["character_slices"] = slices
    result["knowledge_architecture"] = {
        "mode": "scene_local_character_slices_v1",
        "hard_claim_gate": False,
        "raw_chronology_in_writer_packet": False,
        "raw_recent_turns_in_writer_packet": False,
        "raw_hidden_lore_in_writer_packet": False,
        "ordinary_self_card_facts_are_known": True,
        "explicit_self_hidden_branches_are_not_known": True,
        "rule": (
            "Prefer information separation over refusal. A character may freely use normal facts from their own self_profile. "
            "Personal knowledge about other people/events comes from that character's personal_memory or current perception. "
            "Do not make a character ignorant of their own age/name/job merely because personal_memory lacks a duplicate entry."
        ),
    }
    return result
