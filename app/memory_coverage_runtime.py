"""Conservative, non-generative guard against silently forgetting clear promises.

This is a *minimum evidence check*, not a semantic proof that every memory was
captured. It NEVER invents knowledge or assigns another character POV's thoughts.
"""
from __future__ import annotations

import re
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
        if _DURABLE_SPEECH.search(speech["text"]):
            cues.append({
                "speaker": speech["speaker"],
                "quote": speech["text"][:180],
            })
    return cues


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
