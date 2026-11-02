"""Conservative, non-generative guard against silently forgetting clear promises.

This is a *minimum evidence check*, not a semantic proof that every memory was
captured. It NEVER invents knowledge or assigns another character POV's thoughts.
"""
from __future__ import annotations

import re
import hashlib
from typing import Any, Dict, List

from fastapi import HTTPException

from .scene_speech import iter_scene_speech


# Deliberately narrow cues: only *spoken* explicit commitments or secrecy
# instructions, not dialogue suggestions, stage directions or old narrative.
_DURABLE_SPEECH = re.compile(
    r"(?iu)\b(?:обещаю|клянусь|даю\s+слово|договорились|никому\s+не\s+рассказывай)\b"
)
_EVIDENCE_FIELDS = (
    "knowledge_add", "experiences_add", "dialogue_memory_add",
    "knowledge_journal_add", "chronology",
)


def durable_speech_cues(scene_output: str) -> List[Dict[str, str]]:
    # Choices are hypothetical, so they must never trigger persistence checks.
    story = str(scene_output or "").split("Что я могу сделать:", 1)[0]
    cues: List[Dict[str, str]] = []
    for speech in iter_scene_speech(story):
        words = re.sub(r"\*\([^*\r\n]*\)\*", "", speech["text"]).strip()
        if _DURABLE_SPEECH.search(words):
            cues.append({
                "speaker": speech["speaker"],
                "quote": words,
            })
    return cues


def preserve_durable_dialogue(session_id: str, payload: Dict[str, Any]) -> None:
    """Keep the speaker's actual commitment, without another review round.

    This records what was said, never its future outcome, and grants no knowledge
    to listeners: being present does not prove that a character heard the line.
    Unknown/ambiguous speakers remain the writer's responsibility.
    """
    cues = durable_speech_cues(str(payload.get("scene_output") or ""))
    if not cues:
        return
    from . import storage
    root = storage.SESSIONS_DIR / session_id
    extracted = payload.setdefault("extracted", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._apply_character_upserts(storage._load_cards(root, source), extracted)
    aliases: Dict[str, set[str]] = {}
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        for name in [cid, *storage._card_names(card)]:
            key = " ".join(str(name).casefold().replace("ё", "е").split())
            aliases.setdefault(key, set()).add(cid)
    turn = int(storage._read_json(root / "meta.json", {}).get("turn_number", 0) or 0) + 1
    rows = extracted.setdefault("experiences_add", [])
    for cue in cues:
        name = re.sub(r"\([^)]*\)", "", cue["speaker"])
        ids = aliases.get(" ".join(name.casefold().replace("ё", "е").split()), set())
        if len(ids) != 1:
            continue
        cid = next(iter(ids))
        quote = cue["quote"]
        # An exact own-memory record already supplied by the writer needs no copy.
        own_rows = [row for key in _EVIDENCE_FIELDS if key != "chronology"
                    for row in (extracted.get(key) or [])
                    if isinstance(row, dict) and row.get("character_id") == cid]
        if any(quote in str(row.get(key) or "") for row in own_rows
               for key in ("summary", "text", "fact", "content")):
            continue
        digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12]
        rows.append({"character_id": cid, "event_id": f"spoken_commitment_t{turn}_{digest}",
                     "summary": f"Собственные слова: {quote}", "turn": turn})


def validate_durable_dialogue_coverage(payload: Dict[str, Any]) -> None:
    """Block only the provable blind spot: clear spoken promise + no evidence.

    With some evidence supplied, whether it is *complete and accurate* still
    belongs to author/character-specific review and chronological audit.
    """
    cues = durable_speech_cues(str(payload.get("scene_output") or ""))
    if not cues:
        return
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    if any(isinstance(extracted.get(key), list) and extracted[key] for key in _EVIDENCE_FIELDS):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "DURABLE_DIALOGUE_NOT_PERSISTED",
            "message": (
                "Scene contains an explicit spoken promise/agreement but all "
                "memory, dialogue and chronology updates are empty. Record "
                "the actual heard statement for its real participants in "
                "dialogue_memory_add/experiences_add/knowledge_add or chronology. "
                "Never add knowledge to absent characters."
            ),
            "cues": cues[:3],
        },
    )
