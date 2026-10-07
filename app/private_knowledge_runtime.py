from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from fastapi import HTTPException

from . import personal_memory_transport, storage, writer_first_runtime


_SPEECH_RE = re.compile(
    r"(?m)^\\s*\\*\\*(?P<speaker>[^*\\n]+)\\*\\*\\s*(?P<channel>\\([^\\n)]{1,80}\\))?\\s*[—-]\\s*(?P<text>.*)$"
)
_COMMUNICATION_RE = re.compile(
    r"(?iu)\b(?:написать|ответить|отправить|переслать|сказать|сообщить|шепнуть|показать|позвонить)\s+([^\s,.;:()—-]+)"
)
_CHAT_RE = re.compile(
    r"(?iu)\b(?:переписк\w*|чат\w*)\s+(?:с|для)\s+([^\s,.;:()—-]+)"
)
_TRANSFER_RE = re.compile(
    r"(?iu)\b(?:переслать|показать|скинуть|дать\s+прочитать)\s+([^\s,.;:()—-]+)"
)
_REMOTE_MARKER_RE = re.compile(
    r"(?iu)\b(?:сообщен\w*|переписк\w*|чат\w*|смс|звон\w*|телефон\w*|трубк\w*|голосов\w*|видеосвяз\w*|мессендж\w*)\b"
)
_ACTION_PREFIXES = {
    "встать", "сесть", "подойти", "отойти", "пойти", "уйти", "вернуться", "взять", "достать",
    "убрать", "положить", "открыть", "закрыть", "посмотреть", "повернуть", "поднять", "опустить",
    "схватить", "обнять", "поцеловать", "погладить", "залезть", "выйти", "зайти", "пройти",
    "наклониться", "присесть", "лечь", "сместить", "перехватить", "рассмотреть", "закатить",
    "улыбнуться", "усмехнуться", "отвернуться", "продолжить", "продолжать", "продолжая", "остаться",
}
_STOP_WORDS = {
    "который", "которая", "которое", "которые", "чтобы", "потом", "сейчас", "теперь", "снова",
    "вообще", "просто", "только", "очень", "себе", "тебе", "тебя", "меня", "мне", "него", "нему",
    "ней", "нее", "этот", "эта", "это", "того", "там", "тут", "сюда", "туда", "ему", "ей",
    "сказать", "написать", "ответить", "отправить", "показать", "переслать", "сообщить",
    "реакция", "вопрос", "ответ", "смотреть", "посмотреть", "думать", "подумать",
}
_COMMUNICATION_WORDS = {
    "писать", "написать", "ответить", "отправить", "переслать", "сказать", "сообщить",
    "шепнуть", "показать", "позвонить", "сообщение", "переписка", "чат",
}
_EXPLICIT_SINGLE_SECRET_RE = re.compile(
    r"(?iu)\b(?:кодовое\s+слово|секретное\s+слово|парол\w*|пин(?:-?код)?)\s*[:—-]?\s*([a-zа-яё][a-zа-яё-]{3,})\b"
)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _stem(value: str) -> str:
    word = _norm(value).strip(".,!?;:()[]{}'«»")
    for ending in (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
        "ах", "ях", "ом", "ем", "ам", "ям", "ой", "ей", "ую", "юю",
        "ов", "ев", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(word) >= 5 and word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


_STOP_STEMS = {_stem(value) for value in _STOP_WORDS}
_COMMUNICATION_STEMS = {_stem(value) for value in _COMMUNICATION_WORDS}
_ACTION_STEMS = {_stem(value) for value in _ACTION_PREFIXES}


def _terms(text: str) -> set[str]:
    result: set[str] = set()
    for match in re.finditer(r"(?iu)[a-zа-яё][a-zа-яё-]{3,}", str(text or "")):
        raw = _norm(match.group(0))
        stem = _stem(raw)
        if raw in _STOP_WORDS or stem in _STOP_STEMS:
            continue
        if stem:
            result.add(stem)
    return result


def _alias_maps(cards: List[Dict[str, Any]]) -> tuple[Dict[str, str], Dict[str, str], Dict[str, set[str]]]:
    exact: Dict[str, str] = {}
    stems: Dict[str, str] = {}
    aliases_by_id: Dict[str, set[str]] = {}
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        names = [cid, *storage._card_names(card)]
        for value in names:
            norm = _norm(value)
            if not norm:
                continue
            first = norm.split()[0]
            exact[norm] = cid
            exact[first] = cid
            stems[_stem(first)] = cid
            aliases_by_id.setdefault(cid, set()).add(first)
    return exact, stems, aliases_by_id


def _recipient_candidates(token: str) -> List[str]:
    norm = _norm(token)
    candidates = [norm]
    if len(norm) >= 4:
        if norm.endswith("у"):
            base = norm[:-1]
            candidates.extend([base, base + "а", base + "я"])
        elif norm.endswith("е"):
            base = norm[:-1]
            candidates.extend([base, base + "я", base + "а", base + "ь"])
        elif norm.endswith("а"):
            base = norm[:-1]
            candidates.extend([base, base + "я"])
        elif norm.endswith("я"):
            base = norm[:-1]
            candidates.extend([base, base + "а"])
    if len(norm) >= 5:
        for ending in ("ом", "ем", "ой", "ей", "ю"):
            if norm.endswith(ending) and len(norm) - len(ending) >= 3:
                base = norm[:-len(ending)]
                candidates.extend([base, base + "а", base + "я", base + "ь"])
                break
    return list(dict.fromkeys(value for value in candidates if value))


def _resolve_recipient(token: str, exact: Dict[str, str], stems: Dict[str, str]) -> str | None:
    for candidate in _recipient_candidates(token):
        direct = exact.get(candidate)
        if direct:
            return direct
        stemmed = stems.get(_stem(candidate))
        if stemmed:
            return stemmed
    return None


def _looks_like_action(sentence: str) -> bool:
    words = re.findall(r"(?iu)[a-zа-яё][a-zа-яё-]+", str(sentence or ""))[:4]
    if not words:
        return False
    stems = [_stem(word) for word in words[:2]]
    return any(
        stem in _ACTION_STEMS
        or stem.startswith(("продолж", "остан", "вернут", "посмотр", "ответ"))
        for stem in stems
    )


def _communication_payload(stage_text: str, match_end: int) -> tuple[str, int]:
    tail_source = str(stage_text or "")[match_end:]
    leading = len(tail_source) - len(tail_source.lstrip(" \t,:"))
    tail = tail_source.lstrip(" \t,:")
    consumed = leading
    if tail.startswith("-") or tail.startswith("—"):
        tail = tail[1:].strip()
        consumed = len(tail_source) - len(tail)
    if not tail:
        return "", match_end

    parts = re.split(r"(?<=[.!?])\s+", tail)
    kept: List[str] = []
    consumed_text = ""
    for index, part in enumerate(parts):
        clean = part.strip()
        if not clean:
            continue
        if index > 0 and _looks_like_action(clean):
            break
        kept.append(clean)
        if not consumed_text:
            consumed_text = clean
        else:
            consumed_text += " " + clean

    payload = " ".join(kept).strip()
    if not payload:
        return "", match_end
    local = tail.find(consumed_text)
    end = match_end + consumed + max(0, local) + len(consumed_text)
    return payload, end


def extract_private_communications(user_input: str, cards: List[Dict[str, Any]], *, turn_number: int = 0) -> List[Dict[str, Any]]:
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    exact, stems, aliases_by_id = _alias_maps(cards)
    rows: List[Dict[str, Any]] = []

    for stage in mapping.get("stage_directions", []):
        stage_text = str(stage or "")
        for regex in (_COMMUNICATION_RE, _CHAT_RE):
            for match in regex.finditer(stage_text):
                recipient_id = _resolve_recipient(match.group(1), exact, stems)
                if not recipient_id:
                    continue
                payload, span_end = _communication_payload(stage_text, match.end())
                if not payload:
                    continue
                rows.append({
                    "turn_number": int(turn_number or 0),
                    "recipient_id": recipient_id,
                    "recipient_aliases": sorted(aliases_by_id.get(recipient_id, set())),
                    "payload": payload,
                    "terms": sorted(_terms(payload)),
                    "stage_text": stage_text,
                    "span_start": match.start(),
                    "span_end": max(match.end(), span_end),
                })
    return rows


def _pov_id(state: Dict[str, Any]) -> str:
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    return str(pov.get("character_id") or pov.get("id") or "")


def _authorized_corpus(root, character_id: str, cards: List[Dict[str, Any]]) -> str:
    card = next((row for row in cards if storage._card_id(row) == character_id), None)
    pieces: List[str] = []
    if isinstance(card, dict):
        pieces.append(str(card))
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = storage._memory_bucket(memory, character_id)
    journal = bucket.get("knowledge_journal", [])
    if isinstance(journal, list):
        pieces.extend(str(row.get("text") or "") for row in journal if isinstance(row, dict))
    knowledge = bucket.get("knowledge", [])
    if isinstance(knowledge, list):
        pieces.extend(str(row) for row in knowledge)
    dialogue = personal_memory_transport.personal_dialogue_rows(
        bucket.get("dialogue_memory", []),
        owner_id=character_id,
        cards=cards,
    )
    for row in dialogue:
        if row.get("summary"):
            pieces.append(str(row["summary"]))
        for segment in row.get("segments", []) if isinstance(row.get("segments"), list) else []:
            if isinstance(segment, dict) and segment.get("text"):
                pieces.append(str(segment["text"]))
        for key in ("question", "answer", "content", "text"):
            if row.get(key):
                pieces.append(str(row[key]))
    return "\n".join(piece for piece in pieces if piece)


def _resolve_speaker_label(label: str, exact: Dict[str, str], stems: Dict[str, str]) -> str | None:
    normalized = _norm(label)
    candidates = [normalized]
    without_context = _norm(re.sub(r"\([^)]*\)", " ", normalized))
    if without_context and without_context not in candidates:
        candidates.append(without_context)
    for candidate in candidates:
        direct = exact.get(candidate)
        if direct:
            return direct
        first = candidate.split()[0] if candidate else ""
        direct = exact.get(first) or stems.get(_stem(first))
        if direct:
            return direct
    return None


def _speaker_units(scene_output: str, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    exact, stems, _ = _alias_maps(cards)
    result: List[Dict[str, Any]] = []
    for match in _SPEECH_RE.finditer(str(scene_output or "")):
        speaker_name = match.group("speaker").strip()
        channel = str(match.group("channel") or "").strip()
        cid = _resolve_speaker_label(speaker_name, exact, stems)
        if cid:
            speaker_label = " ".join(value for value in (speaker_name, channel) if value)
            result.append({
                "character_id": cid,
                "speaker_label": speaker_label,
                "text": match.group("text").strip(),
                "position": match.start(),
            })
    return result


def _historical_remote_records(
    turn: Dict[str, Any],
    cards: List[Dict[str, Any]],
    *,
    pov_id: str,
) -> List[Dict[str, Any]]:
    extracted = turn.get("extracted") if isinstance(turn.get("extracted"), dict) else {}
    dialogue = extracted.get("dialogue_memory_add")
    if not isinstance(dialogue, list) or not pov_id:
        return []

    _, _, aliases_by_id = _alias_maps(cards)
    result: List[Dict[str, Any]] = []
    turn_number = int(turn.get("turn_number", 0) or 0)
    for row in dialogue:
        if not isinstance(row, dict) or _norm(row.get("mode")) != "remote":
            continue
        raw_participants = row.get("participants") or row.get("participant_ids") or []
        if isinstance(raw_participants, str):
            raw_participants = [raw_participants]
        participants = [str(value) for value in raw_participants if value]
        if pov_id not in participants or len(set(participants)) < 2:
            continue

        participant_aliases: set[str] = set()
        for cid in participants:
            participant_aliases.update(aliases_by_id.get(cid, set()))

        segments = row.get("segments")
        if isinstance(segments, list) and segments:
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                payload = str(segment.get("text") or "").strip()
                if not payload:
                    continue
                result.append({
                    "turn_number": turn_number,
                    "participant_ids": list(dict.fromkeys(participants)),
                    "participant_aliases": sorted(participant_aliases),
                    "payload": payload,
                    "terms": sorted(_terms(payload)),
                    "topic_id": row.get("topic_id"),
                })
            continue

        # Backward-compatible fallback for an already stored remote memory row.
        payload = str(row.get("summary") or "").strip()
        if payload:
            result.append({
                "turn_number": turn_number,
                "participant_ids": list(dict.fromkeys(participants)),
                "participant_aliases": sorted(participant_aliases),
                "payload": payload,
                "terms": sorted(_terms(payload)),
                "topic_id": row.get("topic_id"),
            })
    return result


def _history_turns(root) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    handoff = storage._read_json(root / "handoff_tail.json", [])
    if isinstance(handoff, dict):
        handoff = handoff.get("turns") or handoff.get("recent_turns") or []
    sources = [
        handoff if isinstance(handoff, list) else [],
        storage._read_turns(root),
    ]
    for source in sources:
        for turn in source:
            if not isinstance(turn, dict):
                continue
            key = (
                int(turn.get("turn_number", 0) or 0),
                str(turn.get("user_input") or ""),
                str(turn.get("scene_output") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(turn)
    return result


def _source_turn_for_compact_row(
    row: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not candidates:
        return row
    if len(candidates) == 1:
        return candidates[0]
    user_input = str(row.get("user_input") or "")
    if user_input:
        exact = [
            turn for turn in candidates
            if str(turn.get("user_input") or "") == user_input
        ]
        if len(exact) == 1:
            return exact[0]
    scene_tail = _norm(row.get("scene_tail") or "")
    if scene_tail:
        exact = [
            turn for turn in candidates
            if scene_tail and scene_tail in _norm(turn.get("scene_output") or "")
        ]
        if len(exact) == 1:
            return exact[0]
    return candidates[-1]


def _private_records(root, cards: List[Dict[str, Any]], current_user_input: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    state = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state)
    for turn in _history_turns(root):
        rows.extend(extract_private_communications(
            str(turn.get("user_input") or ""),
            cards,
            turn_number=int(turn.get("turn_number", 0) or 0),
        ))
        rows.extend(_historical_remote_records(turn, cards, pov_id=pov_id))
    if current_user_input:
        meta = storage._read_json(root / "meta.json", {})
        rows.extend(extract_private_communications(
            current_user_input,
            cards,
            turn_number=int(meta.get("turn_number", 0) or 0) + 1,
        ))
    return rows


def _explicit_single_secret_terms(payload: str) -> set[str]:
    result: set[str] = set()
    for match in _EXPLICIT_SINGLE_SECRET_RE.finditer(str(payload or "")):
        result.update(_terms(match.group(1)))
    return result


def _leaked_terms(
    text: str,
    protected_terms: set[str],
    allowed_terms: set[str],
    *,
    protected_payload: str = "",
) -> List[str]:
    used = _terms(text)
    leaked = (used & protected_terms) - allowed_terms
    if len(leaked) >= 2:
        return sorted(leaked)
    if len(leaked) == 1:
        term = next(iter(leaked))
        if term in _explicit_single_secret_terms(protected_payload):
            return [term]
    return []


def _mentions_private_contact(text: str, record: Dict[str, Any]) -> bool:
    used = _terms(text)
    if not (used & _COMMUNICATION_STEMS):
        return False
    normalized = _norm(text)
    aliases = record.get("participant_aliases") or record.get("recipient_aliases") or []
    return any(alias and alias in normalized for alias in aliases)


def _record_participants(record: Dict[str, Any]) -> set[str]:
    values = record.get("participant_ids")
    if isinstance(values, list):
        return {str(value) for value in values if value}
    recipient = str(record.get("recipient_id") or "")
    return {recipient} if recipient else set()


def _record_source_ids(record: Dict[str, Any], *, pov_id: str) -> set[str]:
    return {
        cid for cid in _record_participants(record)
        if cid and cid != pov_id
    }


def _stage_mentions_character(stage_text: str, character_id: str, aliases_by_id: Dict[str, set[str]]) -> bool:
    alias_forms: set[str] = set()
    for alias in aliases_by_id.get(character_id, set()):
        if not alias:
            continue
        alias_forms.update(_recipient_candidates(alias))
    for word in re.findall(r"(?iu)[a-zа-яё][a-zа-яё-]+", str(stage_text or "")):
        candidates = set(_recipient_candidates(word))
        if candidates & alias_forms:
            return True
    return False


def _current_private_transfer_sources(
    user_input: str,
    cards: List[Dict[str, Any]],
    records: List[Dict[str, Any]],
    *,
    pov_id: str,
) -> Dict[str, set[str]]:
    """Return recipient -> private source characters explicitly forwarded/shown this turn.

    This is deliberately narrow: ordinary writing/calling does not grant access to another
    private conversation. The source must be named in the same stage direction.
    """
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    exact, stems, aliases_by_id = _alias_maps(cards)
    result: Dict[str, set[str]] = {}

    for stage in mapping.get("stage_directions", []):
        stage_text = str(stage or "")
        for match in _TRANSFER_RE.finditer(stage_text):
            recipient_id = _resolve_recipient(match.group(1), exact, stems)
            if not recipient_id:
                continue
            sources: set[str] = set()
            for record in records:
                for source_id in _record_source_ids(record, pov_id=pov_id):
                    if source_id == recipient_id:
                        continue
                    if _stage_mentions_character(stage_text, source_id, aliases_by_id):
                        sources.add(source_id)
            if sources:
                result.setdefault(recipient_id, set()).update(sources)
    return result


def _current_transfer_allowed_terms(
    payload: Dict[str, Any],
    records: List[Dict[str, Any]],
    transfer_sources: Dict[str, set[str]],
    *,
    pov_id: str,
) -> Dict[str, set[str]]:
    """Only terms actually written to the recipient's journal become same-turn knowledge."""
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    journal_rows = extracted.get("knowledge_journal_add")
    if not isinstance(journal_rows, list):
        return {}

    protected_by_source: Dict[str, set[str]] = {}
    for record in records:
        protected = set(record.get("terms") or [])
        if not protected:
            continue
        for source_id in _record_source_ids(record, pov_id=pov_id):
            protected_by_source.setdefault(source_id, set()).update(protected)

    result: Dict[str, set[str]] = {}
    for row in journal_rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("character_id") or "")
        sources = transfer_sources.get(cid, set())
        if not cid or not sources:
            continue
        text = str(row.get("text") or row.get("fact") or row.get("summary") or "")
        row_terms = _terms(text)
        allowed = set()
        for source_id in sources:
            allowed.update(row_terms & protected_by_source.get(source_id, set()))
        if allowed:
            result.setdefault(cid, set()).update(allowed)
    return result


def validate_private_knowledge(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state)
    records = _private_records(root, cards, str(payload.get("user_input") or ""))
    if not records:
        return

    transfer_sources = _current_private_transfer_sources(
        str(payload.get("user_input") or ""),
        cards,
        records,
        pov_id=pov_id,
    )
    transfer_allowed_terms = _current_transfer_allowed_terms(
        payload,
        records,
        transfer_sources,
        pov_id=pov_id,
    )

    units = _speaker_units(str(payload.get("scene_output") or ""), cards)
    earlier_public_speech: List[str] = []
    for unit in units:
        cid = str(unit.get("character_id") or "")
        text = str(unit.get("text") or "")
        if not cid:
            continue
        if cid == pov_id:
            earlier_public_speech.append(text)
            continue

        corpus = _authorized_corpus(root, cid, cards)
        allowed_terms = _terms(corpus + "\n" + "\n".join(earlier_public_speech))
        allowed_terms.update(transfer_allowed_terms.get(cid, set()))
        for record in records:
            if cid in _record_participants(record):
                continue
            record_sources = _record_source_ids(record, pov_id=pov_id)
            source_was_transferred = bool(record_sources & transfer_sources.get(cid, set()))
            protected = set(record.get("terms") or [])
            leaked = _leaked_terms(
                text,
                protected,
                allowed_terms,
                protected_payload=str(record.get("payload") or ""),
            )
            contact_leak = _mentions_private_contact(text, record) and not source_was_transferred
            if leaked or contact_leak:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PRIVATE_COMMUNICATION_KNOWLEDGE_LEAK",
                        "character_id": cid,
                        "source_turn": record.get("turn_number"),
                        "leaked_terms": leaked,
                        "instruction": (
                            "Перепиши сцену тем же packet_id: этот NPC использовал содержание или сам факт приватной "
                            "коммуникации, к которой у него нет источника. Chronology/recent_turns не являются его знанием. "
                            "Разреши факт только если персонаж реально узнаёт его в сцене или он уже есть в его personal memory."
                        ),
                    },
                )
        earlier_public_speech.append(text)

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    journal_rows = extracted.get("knowledge_journal_add")
    if isinstance(journal_rows, list):
        for row in journal_rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("character_id") or "")
            if not cid or cid == pov_id:
                continue
            text = str(row.get("text") or row.get("fact") or row.get("summary") or "")
            allowed_terms = _terms(_authorized_corpus(root, cid, cards))
            allowed_terms.update(transfer_allowed_terms.get(cid, set()))
            for record in records:
                if cid in _record_participants(record):
                    continue
                record_sources = _record_source_ids(record, pov_id=pov_id)
                source_was_transferred = bool(record_sources & transfer_sources.get(cid, set()))
                leaked = _leaked_terms(
                    text,
                    set(record.get("terms") or []),
                    allowed_terms,
                    protected_payload=str(record.get("payload") or ""),
                )
                if leaked or (_mentions_private_contact(text, record) and not source_was_transferred):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "PRIVATE_COMMUNICATION_JOURNAL_LEAK",
                            "character_id": cid,
                            "source_turn": record.get("turn_number"),
                            "leaked_terms": leaked,
                            "instruction": "Не сохраняй персонажу приватную коммуникацию, которую он реально не получил.",
                        },
                    )


def normalize_dialogue_memory_modes(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        return result

    dialogue = extracted.get("dialogue_memory_add")
    if not isinstance(dialogue, list) or not dialogue:
        return result

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state_before = storage._read_json(root / "state.json", {})
    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    state_after = storage._deep_merge(state_before, state_patch)
    pov_id = _pov_id(state_before)

    present_ids = {
        str(value)
        for value in [*storage._present_character_ids(state_before), *storage._present_character_ids(state_after)]
        if value
    }
    remote_ids = {
        str(value)
        for value in [*storage._remote_character_ids(state_before), *storage._remote_character_ids(state_after)]
        if value
    }

    units = _speaker_units(str(result.get("scene_output") or ""), cards)
    explicit_remote_ids = {
        str(unit.get("character_id") or "")
        for unit in units
        if unit.get("character_id")
        and _REMOTE_MARKER_RE.search(f"{unit.get('speaker_label') or ''} {unit.get('text') or ''}") is not None
    }
    direct_remote_ids = {
        str(row.get("recipient_id") or "")
        for row in extract_private_communications(str(result.get("user_input") or ""), cards)
        if isinstance(row, dict) and row.get("recipient_id")
    }

    exact, stems, _ = _alias_maps(cards)
    normalized: List[Dict[str, Any]] = []
    for raw in dialogue:
        row = deepcopy(raw) if isinstance(raw, dict) else raw
        if not isinstance(row, dict) or _norm(row.get("mode")) != "remote":
            normalized.append(row)
            continue

        participants = row.get("participants") or row.get("participant_ids") or []
        if isinstance(participants, str):
            participants = [participants]
        resolved: List[str] = []
        for value in participants:
            if not value:
                continue
            cid = _resolve_recipient(str(value), exact, stems) or str(value)
            if cid not in resolved:
                resolved.append(cid)
        counterparts = [cid for cid in resolved if cid != pov_id]
        has_remote_evidence = any(
            cid in remote_ids or cid in explicit_remote_ids or cid in direct_remote_ids
            for cid in counterparts
        )
        if not has_remote_evidence:
            row["mode"] = "physical" if any(cid in present_ids for cid in counterparts) else "dialogue"
        normalized.append(row)

    extracted["dialogue_memory_add"] = normalized
    result["extracted"] = extracted
    return result


def add_direct_communication_memory(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        extracted = {}
        result["extracted"] = extracted

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state)
    rows = extract_private_communications(str(result.get("user_input") or ""), cards)
    if not rows or not pov_id:
        return result

    existing = extracted.get("knowledge_journal_add")
    existing = deepcopy(existing) if isinstance(existing, list) else []
    existing_keys = {
        (str(row.get("character_id") or ""), _norm(row.get("text") or row.get("fact") or ""))
        for row in existing if isinstance(row, dict)
    }

    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    date = current.get("date")
    period = current.get("period") or current.get("day_period")

    for record in rows:
        payload_text = str(record.get("payload") or "").strip()
        recipient_id = str(record.get("recipient_id") or "")
        if not payload_text or not recipient_id:
            continue
        for cid, text in (
            (recipient_id, f"Получена приватная коммуникация от POV: {payload_text}"),
            (pov_id, f"POV отправил приватную коммуникацию персонажу {recipient_id}: {payload_text}"),
        ):
            key = (cid, _norm(text))
            if key in existing_keys:
                continue
            existing.append({
                "character_id": cid,
                "date": date,
                "period": period,
                "text": text,
            })
            existing_keys.add(key)

    extracted["knowledge_journal_add"] = existing
    return result


def _clean_remote_line(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^\s*\*?\([^)]*\)\*?\s*", "", value).strip()
    return " ".join(value.split())


def _journal_contains_message(rows: List[Dict[str, Any]], character_id: str, message: str) -> bool:
    needle = _norm(message)
    if not needle:
        return False
    for row in rows:
        if not isinstance(row, dict) or str(row.get("character_id") or "") != character_id:
            continue
        haystack = _norm(row.get("text") or row.get("fact") or row.get("summary") or "")
        if needle and needle in haystack:
            return True
    return False


def _remote_target_from_label(
    label: str,
    *,
    exact: Dict[str, str],
    stems: Dict[str, str],
    exclude_id: str,
) -> str | None:
    normalized = _norm(label)
    if not normalized:
        return None
    found: List[str] = []
    for alias, cid in exact.items():
        if cid == exclude_id or not alias:
            continue
        if re.search(rf"(?<![a-zа-яё0-9_-]){re.escape(alias)}(?![a-zа-яё0-9_-])", normalized, flags=re.IGNORECASE):
            found.append(cid)
    if len(set(found)) == 1:
        return found[0]
    for token in re.findall(r"(?iu)[a-zа-яё][a-zа-яё-]+", normalized):
        cid = stems.get(_stem(token))
        if cid and cid != exclude_id:
            found.append(cid)
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else None


def add_scene_remote_communication_memory(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    result = deepcopy(payload)
    extracted = result.get("extracted")
    if not isinstance(extracted, dict):
        extracted = {}
        result["extracted"] = extracted

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state_before = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state_before)
    if not pov_id:
        return result

    state_patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    state_after = storage._deep_merge(state_before, state_patch)
    present_ids = {
        str(value)
        for value in [*storage._present_character_ids(state_before), *storage._present_character_ids(state_after)]
        if value
    }
    remote_ids = {
        str(value)
        for value in [*storage._remote_character_ids(state_before), *storage._remote_character_ids(state_after)]
        if value and str(value) != pov_id
    }

    units = _speaker_units(str(result.get("scene_output") or ""), cards)
    exact, stems, _ = _alias_maps(cards)

    explicit_remote_npcs = {
        str(unit.get("character_id") or "")
        for unit in units
        if str(unit.get("character_id") or "") not in {"", pov_id}
        and _REMOTE_MARKER_RE.search(f"{unit.get('speaker_label') or ''} {unit.get('text') or ''}") is not None
    }
    candidate_counterparts = {
        cid for cid in [*remote_ids, *explicit_remote_npcs]
        if cid and cid != pov_id
    }

    exchanges: Dict[str, List[Dict[str, str]]] = {}
    for unit in units:
        cid = str(unit.get("character_id") or "")
        if not cid:
            continue
        label = str(unit.get("speaker_label") or "")
        raw_text = str(unit.get("text") or "")
        marker_text = f"{label} {raw_text}"
        explicit_remote = _REMOTE_MARKER_RE.search(marker_text) is not None
        line = _clean_remote_line(raw_text)
        if not line:
            continue

        if cid == pov_id:
            if not explicit_remote:
                continue
            target_id = _remote_target_from_label(
                label,
                exact=exact,
                stems=stems,
                exclude_id=pov_id,
            )
            if not target_id and len(candidate_counterparts) == 1:
                target_id = next(iter(candidate_counterparts))
            if not target_id:
                continue
            bucket = exchanges.setdefault(target_id, [])
            item = {"speaker_id": pov_id, "text": line}
            if item not in bucket:
                bucket.append(item)
            continue

        # An explicit remote marker wins even if the same NPC physically enters later
        # in the turn. Without an explicit marker, only an exclusively remote NPC is
        # safe to classify as remote communication.
        if not explicit_remote:
            if cid not in remote_ids or cid in present_ids:
                continue

        bucket = exchanges.setdefault(cid, [])
        item = {"speaker_id": cid, "text": line}
        if item not in bucket:
            bucket.append(item)

    if not exchanges:
        return result

    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1
    current = state_after.get("current") if isinstance(state_after.get("current"), dict) else {}
    date = current.get("date")
    period = current.get("period") or current.get("day_period")

    journal = extracted.get("knowledge_journal_add")
    journal = deepcopy(journal) if isinstance(journal, list) else []

    dialogue = extracted.get("dialogue_memory_add")
    dialogue = deepcopy(dialogue) if isinstance(dialogue, list) else []
    dialogue_ids = {
        str(row.get("topic_id") or "")
        for row in dialogue if isinstance(row, dict) and row.get("topic_id")
    }

    for counterpart_id, rows in exchanges.items():
        summary_parts: List[str] = []
        missing_by_owner: Dict[str, List[Dict[str, str]]] = {pov_id: [], counterpart_id: []}

        for row in rows:
            speaker_id = str(row.get("speaker_id") or "")
            line = str(row.get("text") or "").strip()
            if not speaker_id or not line:
                continue
            rendered = f"{'POV' if speaker_id == pov_id else 'Собеседник'}: {line}"
            summary_parts.append(rendered)

            for owner_id in (pov_id, counterpart_id):
                if not _journal_contains_message(journal, owner_id, line):
                    missing_by_owner[owner_id].append({"speaker_id": speaker_id, "text": line})

        content = " ".join(summary_parts).strip()[:4000]
        if not content:
            continue

        for owner_id, missing_rows in missing_by_owner.items():
            if not missing_rows:
                continue
            personal_parts = [
                f"{'Я' if str(item.get('speaker_id') or '') == owner_id else 'Собеседник'}: {str(item.get('text') or '').strip()}"
                for item in missing_rows
                if str(item.get("text") or "").strip()
            ]
            journal.append({
                "character_id": owner_id,
                "date": date,
                "period": period,
                "text": f"Коммуникация: {' '.join(personal_parts)[:4000]}",
            })

        topic_id = f"remote_t{turn_number}_{counterpart_id}"
        if topic_id not in dialogue_ids:
            dialogue.append({
                "topic_id": topic_id,
                "participants": [pov_id, counterpart_id],
                "mode": "remote",
                "summary": content,
                "segments": deepcopy(rows),
                "turn": turn_number,
            })
            dialogue_ids.add(topic_id)

    extracted["knowledge_journal_add"] = journal
    extracted["dialogue_memory_add"] = dialogue
    return result



def _redact_text(text: str, records: List[Dict[str, Any]]) -> str:
    result = str(text or "")
    for record in records:
        payload = str(record.get("payload") or "").strip()
        if payload:
            result = re.sub(
                re.escape(payload),
                "[содержание приватной коммуникации скрыто; используй personal memory участников]",
                result,
                flags=re.IGNORECASE,
            )
    return result


def redact_private_history(context: Dict[str, Any], *, root, cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = deepcopy(context)
    state = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state)
    stored_turns: Dict[int, List[Dict[str, Any]]] = {}
    for turn in _history_turns(root):
        number = int(turn.get("turn_number", 0) or 0)
        if number > 0:
            stored_turns.setdefault(number, []).append(turn)
    total = 0
    for key in ("recent_turns", "continuity_turns"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        cleaned = []
        for raw in rows:
            row = deepcopy(raw) if isinstance(raw, dict) else raw
            if not isinstance(row, dict):
                cleaned.append(row)
                continue

            records = extract_private_communications(
                str(row.get("user_input") or ""),
                cards,
                turn_number=int(row.get("turn_number", 0) or 0),
            )
            candidates = stored_turns.get(int(row.get("turn_number", 0) or 0), [])
            source_turn = _source_turn_for_compact_row(row, candidates)
            remote_records = _historical_remote_records(source_turn, cards, pov_id=pov_id)
            records.extend(remote_records)

            if records:
                total += len(records)
                row["user_input"] = _redact_text(str(row.get("user_input") or ""), records)
                if "scene_output" in row:
                    row["scene_output"] = _redact_text(str(row.get("scene_output") or ""), records)
                if "scene_tail" in row:
                    row["scene_tail"] = _redact_text(str(row.get("scene_tail") or ""), records)

            extracted = row.get("extracted")
            if isinstance(extracted, dict):
                dialogue = extracted.get("dialogue_memory_add")
                if isinstance(dialogue, list):
                    cleaned_dialogue = []
                    for item in dialogue:
                        value = deepcopy(item) if isinstance(item, dict) else item
                        if isinstance(value, dict) and _norm(value.get("mode")) == "remote":
                            if "summary" in value:
                                value["summary"] = "[содержание приватной коммуникации скрыто; используй personal memory участников]"
                            value.pop("segments", None)
                        cleaned_dialogue.append(value)
                    extracted["dialogue_memory_add"] = cleaned_dialogue
                row["extracted"] = extracted

            cleaned.append(row)
        result[key] = cleaned

    result["private_communication_boundary"] = {
        "historical_private_segments_redacted": total,
        "rule": (
            "Private messages/calls/shown content from prior turns are not shared character knowledge. "
            "Their content is available only through the personal memory of characters who actually received it."
        ),
    }
    return result
