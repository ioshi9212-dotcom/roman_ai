from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import private_knowledge_runtime, profile_templates, storage, writer_first_runtime


_VERSION = 1
_NUMBER_RE = re.compile(r"(?<!\w)\d{1,4}(?!\w)")
_INFERENCE_MARKERS = (
    "может", "возможно", "похоже", "кажется", "наверное", "вероятно",
    "думаю", "предполага", "если я правильно", "выходит",
)


def _fact_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("text", "fact", "summary", "event", "description", "content", "memory", "note", "detail"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _memory_rows(root, character_id: str) -> List[str]:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    bucket = characters.get(character_id) if isinstance(characters.get(character_id), dict) else {}
    rows: List[str] = []

    journal = bucket.get("knowledge_journal")
    if isinstance(journal, list):
        rows.extend(
            str(row.get("text") or "").strip()
            for row in journal
            if isinstance(row, dict) and str(row.get("text") or "").strip()
        )

    knowledge = bucket.get("knowledge")
    if isinstance(knowledge, list):
        rows.extend(text for text in (_fact_text(row) for row in knowledge) if text)

    return list(dict.fromkeys(rows))


def _authorized_corpus(root, character_id: str, cards: List[Dict[str, Any]]) -> str:
    card = next((row for row in cards if storage._card_id(row) == character_id), None)
    pieces: List[str] = []
    if isinstance(card, dict):
        # Current v5 profiles are the character's self-known profile. Hidden/director
        # facts live outside this rendered profile and are not added here.
        rendered = profile_templates.render_character_profile(card)
        if rendered:
            pieces.append(rendered)
    pieces.extend(_memory_rows(root, character_id))
    return "\n".join(piece for piece in pieces if piece)


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        preferred = _fact_text(value)
        if preferred:
            yield preferred
            return
        for child in value.values():
            yield from _iter_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from _iter_strings(child)


def _author_only_rows(root) -> List[Dict[str, str]]:
    source = storage._read_json(root / "source.json", {})
    rows: List[Dict[str, str]] = []
    for key in ("hidden_lore", "lore", "foundation", "story_direction", "world"):
        for text in _iter_strings(source.get(key)):
            rows.append({"source": f"author:{key}", "text": text})

    chronology = storage._read_json(root / "chronology.json", [])
    for text in _iter_strings(chronology):
        rows.append({"source": "chronology", "text": text})
    return rows


def _prior_scene_rows(root) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    turns = storage._read_turns(root)
    for turn in turns[-8:]:
        scene = str(turn.get("scene_output") or "")
        if not scene:
            continue
        for unit in private_knowledge_runtime._speaker_units(scene, storage._load_cards(root, storage._read_json(root / "source.json", {}))):
            text = str(unit.get("text") or "").strip()
            if text:
                rows.append({
                    "source": f"prior_scene_output:{unit.get('character_id') or 'unknown'}",
                    "text": text,
                })
    return rows


def _protected_rows(root, character_id: str, cards: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    rows: List[Dict[str, str]] = []

    for source_character_id in characters:
        source_character_id = str(source_character_id)
        if source_character_id == character_id:
            continue
        for text in _memory_rows(root, source_character_id):
            rows.append({"source": f"character:{source_character_id}", "text": text})

    rows.extend(_author_only_rows(root))
    rows.extend(_prior_scene_rows(root))
    return rows


def _numbers(text: str) -> set[str]:
    return {match.group(0) for match in _NUMBER_RE.finditer(str(text or ""))}


def _looks_like_inference(text: str) -> bool:
    normalized = private_knowledge_runtime._norm(text)
    return "?" in str(text or "") or any(marker in normalized for marker in _INFERENCE_MARKERS)


def _can_be_supported_inference(
    text: str,
    *,
    leaked_terms: set[str],
    leaked_numbers: set[str],
    allowed_terms: set[str],
) -> bool:
    if leaked_numbers or len(leaked_terms) != 1 or not _looks_like_inference(text):
        return False
    premises = private_knowledge_runtime._terms(text) - leaked_terms
    return bool(premises & allowed_terms)


def _current_input_access(user_input: str, cards: List[Dict[str, Any]]) -> tuple[str, Dict[str, str]]:
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    public_text = " ".join(str(value) for value in mapping.get("spoken_segments", []) if value)
    recipient_text: Dict[str, List[str]] = {}
    for row in private_knowledge_runtime.extract_private_communications(str(user_input or ""), cards):
        cid = str(row.get("recipient_id") or "")
        payload = str(row.get("payload") or "").strip()
        if cid and payload:
            recipient_text.setdefault(cid, []).append(payload)
    return public_text, {
        cid: "\n".join(values)
        for cid, values in recipient_text.items()
    }


def _can_hear(
    listener_id: str,
    speaker_id: str,
    *,
    pov_id: str,
    present: set[str],
    remote: set[str],
) -> bool:
    if listener_id == speaker_id:
        return True
    if listener_id in present and speaker_id in present:
        return True
    if listener_id in remote and speaker_id == pov_id:
        return True
    if listener_id == pov_id and speaker_id in remote:
        return True
    return False


def _unsupported_match(
    text: str,
    protected_text: str,
    *,
    allowed_text: str,
    allowed_terms: set[str],
) -> tuple[set[str], set[str]]:
    protected_terms = private_knowledge_runtime._terms(protected_text)
    used_terms = private_knowledge_runtime._terms(text)
    leaked_terms = (used_terms & protected_terms) - allowed_terms

    allowed_numbers = _numbers(allowed_text)
    leaked_numbers = (_numbers(text) & _numbers(protected_text)) - allowed_numbers

    # Two matching concrete terms are a specificity leak. One long distinctive
    # term is enough only when it is very specific; numbers are always exact.
    strong_single = {term for term in leaked_terms if len(term) >= 9}
    if leaked_numbers or len(leaked_terms) >= 2 or strong_single:
        return leaked_terms, leaked_numbers
    return set(), set()


def build_boundaries(
    root,
    character_ids: List[str],
    *,
    cards: List[Dict[str, Any]] | None = None,
    pov_id: str = "",
) -> Dict[str, Any]:
    source = storage._read_json(root / "source.json", {})
    cards = cards if cards is not None else storage._load_cards(root, source)
    characters: Dict[str, Any] = {}

    for character_id in character_ids:
        cid = str(character_id)
        if not cid or cid == pov_id:
            continue
        allowed_text = _authorized_corpus(root, cid, cards)
        allowed_terms = private_knowledge_runtime._terms(allowed_text)
        may_know = _memory_rows(root, cid)[-16:]

        must_not: List[Dict[str, str]] = []
        for row in _protected_rows(root, cid, cards):
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            unknown = private_knowledge_runtime._terms(text) - allowed_terms
            unknown_numbers = _numbers(text) - _numbers(allowed_text)
            if len(unknown) < 2 and not unknown_numbers:
                continue
            must_not.append({
                "source": str(row.get("source") or "unknown"),
                "fact": text[:320],
            })
            if len(must_not) >= 16:
                break

        characters[cid] = {
            "may_know": may_know,
            "must_not_know": must_not,
            "authority": [
                f"character_cards[character_id={cid}] self-known facts",
                f"character_memory[{cid}].knowledge",
                f"character_memory[{cid}].knowledge_journal",
                "information actually perceived/heard/read/received earlier in the current turn",
            ],
        }

    return {
        "version": _VERSION,
        "mandatory": True,
        "epistemic_authority": "personal knowledge + self-known profile + current-turn perception only",
        "narrative_continuity_only_not_knowledge": [
            "chronology_recent",
            "recent_turns",
            "previous scene_output",
            "another character's knowledge",
            "hidden_lore/director context",
            "location/canon context unless actually perceived or told",
        ],
        "inference_rule": (
            "Unknown detail may appear only as an explicit suspicion/question when its premise is already known to that NPC. "
            "A guess is not established knowledge and cannot add hidden specificity without a known premise."
        ),
        "characters": characters,
    }


def validate_scene_output(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    present = {str(value) for value in storage._present_character_ids(state) if value}
    remote = {str(value) for value in storage._remote_character_ids(state) if value}

    user_input = str(payload.get("user_input") or "")
    public_input_text, recipient_text = _current_input_access(user_input, cards)
    private_records = private_knowledge_runtime._private_records(root, cards, user_input)
    transfer_sources = private_knowledge_runtime._current_private_transfer_sources(
        user_input,
        cards,
        private_records,
        pov_id=pov_id,
    )
    transfer_allowed_terms = private_knowledge_runtime._current_transfer_allowed_terms(
        payload,
        private_records,
        transfer_sources,
        pov_id=pov_id,
    )
    units = private_knowledge_runtime._speaker_units(str(payload.get("scene_output") or ""), cards)
    earlier_speech: List[Dict[str, str]] = []

    protected_cache: Dict[str, List[Dict[str, str]]] = {}
    base_text_cache: Dict[str, str] = {}

    for unit in units:
        cid = str(unit.get("character_id") or "")
        text = str(unit.get("text") or "").strip()
        if not cid or not text:
            continue

        if cid == pov_id:
            earlier_speech.append({"character_id": cid, "text": text})
            continue

        base_text = base_text_cache.setdefault(cid, _authorized_corpus(root, cid, cards))
        heard = [
            row["text"]
            for row in earlier_speech
            if _can_hear(
                cid,
                str(row.get("character_id") or ""),
                pov_id=pov_id,
                present=present,
                remote=remote,
            )
        ]
        allowed_text = "\n".join([
            base_text,
            public_input_text,
            recipient_text.get(cid, ""),
            *heard,
        ])
        allowed_terms = private_knowledge_runtime._terms(allowed_text)
        allowed_terms.update(transfer_allowed_terms.get(cid, set()))

        unsupported: List[Dict[str, Any]] = []
        leaked_terms_all: set[str] = set()
        leaked_numbers_all: set[str] = set()

        for row in protected_cache.setdefault(cid, _protected_rows(root, cid, cards)):
            protected_text = str(row.get("text") or "").strip()
            if not protected_text:
                continue
            leaked_terms, leaked_numbers = _unsupported_match(
                text,
                protected_text,
                allowed_text=allowed_text,
                allowed_terms=allowed_terms,
            )
            if not leaked_terms and not leaked_numbers:
                continue
            if _can_be_supported_inference(
                text,
                leaked_terms=leaked_terms,
                leaked_numbers=leaked_numbers,
                allowed_terms=allowed_terms,
            ):
                continue
            leaked_terms_all.update(leaked_terms)
            leaked_numbers_all.update(leaked_numbers)
            unsupported.append({
                "source": str(row.get("source") or "unknown"),
                "fact": protected_text[:320],
            })
            if len(unsupported) >= 3:
                break

        if unsupported:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SCENE_NPC_KNOWLEDGE_LEAK",
                    "character_id": cid,
                    "unsupported_text": text,
                    "unsupported_facts": unsupported,
                    "leaked_terms": sorted(leaked_terms_all),
                    "leaked_numbers": sorted(leaked_numbers_all),
                    "instruction": (
                        "Rewrite only the unsupported NPC line and retry the same pending turn/packet_id. "
                        "chronology, recent/previous scene_output, hidden/director context and another character's knowledge "
                        "are narrative context, not this NPC's epistemic authority."
                    ),
                },
            )

        earlier_speech.append({"character_id": cid, "text": text})
