from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from . import storage


_IDENTITY_KEYS = {
    "participants",
    "participant_ids",
    "speaker_id",
    "speaker",
    "listener",
    "asked_by",
    "asked_to",
    "said_by",
    "heard_by",
    "owner_character_id",
    "target_character_id",
    "character_id",
}


def _aliases_for(cards: Iterable[Dict[str, Any]], character_id: str) -> List[str]:
    for card in cards:
        if storage._card_id(card) == character_id:
            values = [character_id, *storage._card_names(card)]
            return list(dict.fromkeys(str(value) for value in values if value))
    return [character_id] if character_id else []


def _replace_speaker_labels(
    text: Any,
    *,
    owner_id: str,
    participant_ids: Iterable[str],
    cards: List[Dict[str, Any]],
) -> str:
    value = str(text or "")
    participant_ids = [str(cid) for cid in participant_ids if cid]

    aliases: List[tuple[str, str]] = []
    for cid in participant_ids:
        replacement = "Я" if cid == owner_id else "Собеседник"
        for alias in _aliases_for(cards, cid):
            aliases.append((alias, replacement))

    # Only replace structural speaker labels, not names genuinely spoken inside dialogue.
    for alias, replacement in sorted(aliases, key=lambda row: len(row[0]), reverse=True):
        pattern = re.compile(rf"(?iu)(?<!\w){re.escape(alias)}(?=\s*[:—-])")
        value = pattern.sub(replacement, value)

    # Canonical ids are never character knowledge merely because memory metadata stores them.
    for cid in participant_ids:
        if not cid:
            continue
        replacement = "я" if cid == owner_id else "собеседник"
        value = re.sub(rf"(?iu)(?<!\w){re.escape(cid)}(?!\w)", replacement, value)

    return value


def personal_dialogue_record(
    record: Any,
    *,
    owner_id: str,
    cards: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        return {}

    raw_participants = record.get("participants") or record.get("participant_ids") or []
    if isinstance(raw_participants, str):
        raw_participants = [raw_participants]
    participant_ids = [str(value) for value in raw_participants if value]

    result: Dict[str, Any] = {}
    for key, value in record.items():
        if key in _IDENTITY_KEYS or key == "topic_id":
            continue
        if key == "segments" and isinstance(value, list):
            segments: List[Dict[str, Any]] = []
            for raw in value:
                if not isinstance(raw, dict):
                    continue
                speaker_id = str(raw.get("speaker_id") or raw.get("character_id") or "")
                text = str(raw.get("text") or raw.get("content") or "").strip()
                if not text:
                    continue
                segments.append({
                    "speaker": "self" if speaker_id == owner_id else "counterpart",
                    "text": text,
                })
            if segments:
                result["segments"] = segments
            continue
        if key == "summary":
            result[key] = _replace_speaker_labels(
                value,
                owner_id=owner_id,
                participant_ids=participant_ids,
                cards=cards,
            )
            continue
        result[key] = deepcopy(value)

    return result


def personal_dialogue_rows(
    rows: Any,
    *,
    owner_id: str,
    cards: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: List[Dict[str, Any]] = []
    for row in rows:
        safe = personal_dialogue_record(row, owner_id=owner_id, cards=cards)
        if safe:
            result.append(safe)
    return result


def neutralize_generated_remote_journal(
    text: Any,
    *,
    owner_id: str,
    cards: List[Dict[str, Any]],
) -> str:
    value = str(text or "").strip()
    if not value.startswith("Удалённая коммуникация с "):
        return value

    # Old backend rows exposed a card display name in the header.
    value = re.sub(
        r"^Удалённая коммуникация с [^:]+:\s*",
        "Коммуникация: ",
        value,
        count=1,
    )

    all_ids = [storage._card_id(card) for card in cards if storage._card_id(card)]
    return _replace_speaker_labels(
        value,
        owner_id=owner_id,
        participant_ids=all_ids,
        cards=cards,
    )
