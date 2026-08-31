from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List


INTRO_MARKERS = (
    "познаком",
    "представил",
    "представила",
    "представился",
    "представилась",
    "обменялись имен",
    "назвал свое имя",
    "назвала свое имя",
    "узнал имя",
    "узнала имя",
    "меня зовут",
    "introduced",
    "met for the first time",
    "exchanged names",
)

NEGATIVE_INTRO_MARKERS = (
    "не познаком",
    "не представ",
    "не успел познаком",
    "не успела познаком",
)

FAMILIARITY_RANK = {
    "not_encountered": 0,
    "encountered": 1,
    "known": 2,
    "acquainted": 3,
}


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def card_id(card: Dict[str, Any]) -> str:
    return str(card.get("character_id") or card.get("id") or card.get("name") or "").strip()


def card_name(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    for value in (
        card.get("name"),
        card.get("full_name"),
        identity.get("full_name"),
        identity.get("name"),
    ):
        if value:
            return " ".join(str(value).split())
    given = " ".join(str(identity.get("given_name") or "").split())
    family = " ".join(str(identity.get("family_name") or "").split())
    return " ".join(part for part in (given, family) if part) or card_id(card)


def card_aliases(card: Dict[str, Any]) -> List[str]:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    values: List[str] = []
    for value in (
        card_id(card),
        card.get("name"),
        card.get("full_name"),
        card.get("short_name"),
        identity.get("name"),
        identity.get("full_name"),
        identity.get("given_name"),
    ):
        if value:
            values.append(str(value))
    aliases = card.get("aliases") or identity.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    values.extend(str(value) for value in aliases if value)
    display = card_name(card)
    if display:
        values.append(display)
        values.append(display.split()[0])
    return list(dict.fromkeys(value for value in values if value))


def short_role(card: Dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    candidates = (
        card.get("card_hint"),
        card.get("short_role"),
        card.get("role"),
        card.get("story_role"),
        identity.get("role"),
        card.get("occupation"),
        identity.get("occupation"),
    )
    raw = next((str(value).strip() for value in candidates if value), "")
    if not raw:
        return ""
    first_line = raw.splitlines()[0].strip()
    sentence_end = first_line.find(".")
    if sentence_end >= 0:
        first_line = first_line[: sentence_end + 1]
    return first_line[:220].rstrip()


def _bucket_text(bucket: Dict[str, Any]) -> str:
    return normalize_name(json.dumps(bucket, ensure_ascii=False, separators=(",", ":")))


def _memory_knows_identity(pov_bucket: Dict[str, Any], character_id: str, aliases: List[str]) -> bool:
    text = _bucket_text(pov_bucket)
    if normalize_name(character_id) and normalize_name(character_id) in text:
        return True
    for alias in aliases:
        normalized = normalize_name(alias)
        if normalized and len(normalized) >= 3 and normalized in text:
            return True
    return False


def _text_explicitly_introduces(text: str, aliases: List[str]) -> bool:
    normalized = normalize_name(text)
    if any(marker in normalized for marker in NEGATIVE_INTRO_MARKERS):
        return False
    if not any(normalize_name(marker) in normalized for marker in INTRO_MARKERS):
        return False
    normalized_aliases = [normalize_name(alias) for alias in aliases if len(normalize_name(alias)) >= 2]
    return any(alias in normalized for alias in normalized_aliases)


def _chronology_introduction(chronology: Any, aliases: List[str]) -> Dict[str, Any] | None:
    if not isinstance(chronology, list):
        return None
    for event in chronology:
        if not isinstance(event, dict):
            continue
        text = " ".join(str(event.get(key) or "") for key in ("event", "summary", "fact", "description"))
        if _text_explicitly_introduces(text, aliases):
            return {
                "turn": event.get("turn") or event.get("turn_number"),
                "source": "chronology_introduction",
            }
    return None


def _turn_introduction(turns: Any, aliases: List[str]) -> Dict[str, Any] | None:
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = f"{turn.get('user_input', '')} {turn.get('scene_output', '')}"
        if _text_explicitly_introduces(text, aliases):
            return {
                "turn": turn.get("turn_number"),
                "source": "shown_scene_introduction",
            }
    return None


def _card_initial_familiarity(card: Dict[str, Any]) -> Dict[str, Any] | None:
    raw = card.get("pov_familiarity")
    if isinstance(raw, dict) and raw.get("status"):
        return deepcopy(raw)
    if isinstance(raw, str) and raw:
        return {"status": raw, "source": "card"}
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    if card.get("known_to_pov") is True or identity.get("known_to_pov") is True:
        return {"status": "known", "source": "starting_canon"}
    return None


def _relationship_evidence(state: Dict[str, Any], character_id: str) -> bool:
    relationships = state.get("relationships", {}) if isinstance(state, dict) else {}
    if not isinstance(relationships, dict):
        return False
    value = relationships.get(character_id)
    if value is None:
        value = relationships.get(f"{character_id}->pov")
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "", 0, False)


def _prefer(existing: Dict[str, Any] | None, candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(existing, dict) or not existing.get("status"):
        return deepcopy(candidate)
    old_status = str(existing.get("status"))
    new_status = str(candidate.get("status"))
    if FAMILIARITY_RANK.get(new_status, -1) > FAMILIARITY_RANK.get(old_status, -1):
        merged = deepcopy(existing)
        merged.update(candidate)
        return merged
    return deepcopy(existing)


def refresh_pov_familiarity(
    cards: List[Dict[str, Any]],
    state: Dict[str, Any],
    memory: Dict[str, Any],
    chronology: Any,
    turns: Any,
    current_turn: int,
) -> Dict[str, Any]:
    result = deepcopy(state) if isinstance(state, dict) else {}
    if not isinstance(result.get("characters"), dict):
        result["characters"] = {}
    pov = result.get("pov") if isinstance(result.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    if not pov_id:
        return result

    memory_characters = memory.get("characters", {}) if isinstance(memory, dict) and isinstance(memory.get("characters"), dict) else {}
    pov_bucket = memory_characters.get(pov_id, {}) if isinstance(memory_characters.get(pov_id), dict) else {}
    present = set()
    current = result.get("current") if isinstance(result.get("current"), dict) else {}
    raw_present = current.get("present_characters", [])
    if isinstance(raw_present, dict):
        raw_present = list(raw_present.keys())
    if isinstance(raw_present, str):
        raw_present = [raw_present]
    if isinstance(raw_present, list):
        for value in raw_present:
            if isinstance(value, dict):
                value = value.get("character_id") or value.get("id") or value.get("name")
            if value:
                present.add(str(value))

    for card in cards:
        cid = card_id(card)
        if not cid or cid == pov_id:
            continue
        info = result["characters"].setdefault(cid, {})
        if not isinstance(info, dict):
            info = {}
            result["characters"][cid] = info

        existing = info.get("pov_familiarity") if isinstance(info.get("pov_familiarity"), dict) else None
        initial = _card_initial_familiarity(card)
        if initial:
            existing = _prefer(existing, initial)

        aliases = card_aliases(card)
        intro = _chronology_introduction(chronology, aliases) or _turn_introduction(turns, aliases)
        if intro:
            existing = _prefer(existing, {
                "status": "acquainted",
                "source": intro["source"],
                "established_turn": intro.get("turn"),
            })
        elif _memory_knows_identity(pov_bucket, cid, aliases):
            existing = _prefer(existing, {
                "status": "known",
                "source": "pov_memory_identity",
            })
        elif _relationship_evidence(result, cid):
            existing = _prefer(existing, {
                "status": "known",
                "source": "legacy_relationship_evidence",
            })

        seen_turn = info.get("last_seen_turn")
        shared = cid in present or bool(seen_turn)
        if shared:
            if not info.get("first_seen_turn"):
                info["first_seen_turn"] = int(seen_turn or current_turn or 0)
            existing = _prefer(existing, {
                "status": "encountered",
                "source": "shared_scene",
                "first_encounter_turn": info.get("first_seen_turn"),
            })

        if not existing:
            existing = {"status": "not_encountered", "source": "none"}
        info["pov_familiarity"] = existing

    return result


def build_character_registry(cards: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    runtime = state.get("characters", {}) if isinstance(state, dict) and isinstance(state.get("characters"), dict) else {}
    result: List[Dict[str, Any]] = []
    for card in cards:
        cid = card_id(card)
        if not cid:
            continue
        info = runtime.get(cid, {}) if isinstance(runtime.get(cid), dict) else {}
        familiarity = info.get("pov_familiarity") if isinstance(info.get("pov_familiarity"), dict) else None
        result.append({
            "character_id": cid,
            "name": card_name(card),
            "role": short_role(card),
            "story_status": info.get("status") or card.get("status") or card.get("story_status") or "active",
            "pov_familiarity": deepcopy(familiarity),
        })
    return result


def registry_instruction() -> str:
    return (
        "CHARACTER REGISTRY is the authoritative live roster and must be checked every turn before introducing, naming, "
        "recognizing or reintroducing a person. character_id links to the full card. New recurring/important named NPCs "
        "must be persisted with character_upserts and will appear in this registry on following turns. "
        "pov_familiarity is persistent continuity, not flavor: not_encountered means POV has never met this person; "
        "encountered means they shared a scene but identity/name may still be unknown; known means POV knows the identity; "
        "acquainted means an introduction/acquaintance is already established. Never stage a first introduction for known or "
        "acquainted characters. Never silently reuse an existing registered name/character_id for an anonymous newcomer. "
        "If an existing registered character enters from offscreen, load that character's full bundle before writing the entrance."
    )
