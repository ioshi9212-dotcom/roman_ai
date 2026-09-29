from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List

from fastapi import HTTPException

from . import storage, writer_first_runtime


_SPEECH_RE = re.compile(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*[—-]\s*(?P<text>.*)$")
_COMMUNICATION_RE = re.compile(
    r"(?iu)\b(?:написать|ответить|отправить|переслать|сказать|сообщить|шепнуть|показать|позвонить)\s+([^\s,.;:()—-]+)"
)
_CHAT_RE = re.compile(
    r"(?iu)\b(?:переписк\w*|чат\w*)\s+(?:с|для)\s+([^\s,.;:()—-]+)"
)
_REMOTE_MARKER_RE = re.compile(
    r"(?iu)\b(?:сообщен\w*|переписк\w*|чат\w*|смс|звон\w*|голосов\w*|видеосвяз\w*|мессендж\w*)\b"
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


def _resolve_recipient(token: str, exact: Dict[str, str], stems: Dict[str, str]) -> str | None:
    norm = _norm(token)
    return exact.get(norm) or stems.get(_stem(norm))


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
    dialogue = bucket.get("dialogue_memory", [])
    if isinstance(dialogue, list):
        pieces.extend(str(row) for row in dialogue if isinstance(row, dict))
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
        speaker_label = match.group("speaker").strip()
        cid = _resolve_speaker_label(speaker_label, exact, stems)
        if cid:
            result.append({
                "character_id": cid,
                "speaker_label": speaker_label,
                "text": match.group("text").strip(),
                "position": match.start(),
            })
    return result


def _private_records(root, cards: List[Dict[str, Any]], current_user_input: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for turn in storage._read_turns(root):
        if not isinstance(turn, dict):
            continue
        rows.extend(extract_private_communications(
            str(turn.get("user_input") or ""),
            cards,
            turn_number=int(turn.get("turn_number", 0) or 0),
        ))
    if current_user_input:
        meta = storage._read_json(root / "meta.json", {})
        rows.extend(extract_private_communications(
            current_user_input,
            cards,
            turn_number=int(meta.get("turn_number", 0) or 0) + 1,
        ))
    return rows


def _leaked_terms(text: str, protected_terms: set[str], allowed_terms: set[str]) -> List[str]:
    used = _terms(text)
    leaked = (used & protected_terms) - allowed_terms
    strong = sorted(term for term in leaked if len(term) >= 7)
    if strong:
        return strong
    return sorted(leaked) if len(leaked) >= 2 else []


def _mentions_private_contact(text: str, record: Dict[str, Any]) -> bool:
    used = _terms(text)
    if not (used & _COMMUNICATION_STEMS):
        return False
    normalized = _norm(text)
    return any(alias and alias in normalized for alias in record.get("recipient_aliases", []))


def validate_private_knowledge(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov_id = _pov_id(state)
    records = _private_records(root, cards, str(payload.get("user_input") or ""))
    if not records:
        return

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
        for record in records:
            if cid == str(record.get("recipient_id") or ""):
                continue
            protected = set(record.get("terms") or [])
            leaked = _leaked_terms(text, protected, allowed_terms)
            contact_leak = _mentions_private_contact(text, record)
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
            for record in records:
                if cid == str(record.get("recipient_id") or ""):
                    continue
                leaked = _leaked_terms(text, set(record.get("terms") or []), allowed_terms)
                if leaked or _mentions_private_contact(text, record):
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

    names = {
        storage._card_id(card): storage._card_name(card)
        for card in cards
        if storage._card_id(card)
    }
    pov_name = names.get(pov_id, "POV")

    for counterpart_id, rows in exchanges.items():
        counterpart_name = names.get(counterpart_id, counterpart_id)
        summary_parts: List[str] = []

        for row in rows:
            speaker_id = str(row.get("speaker_id") or "")
            line = str(row.get("text") or "").strip()
            if not speaker_id or not line:
                continue
            speaker_name = names.get(speaker_id, pov_name if speaker_id == pov_id else speaker_id)
            summary_parts.append(f"{speaker_name}: {line}")

            for owner_id in (pov_id, counterpart_id):
                if _journal_contains_message(journal, owner_id, line):
                    continue
                other_name = counterpart_name if owner_id == pov_id else pov_name
                journal.append({
                    "character_id": owner_id,
                    "date": date,
                    "period": period,
                    "text": f"Удалённая коммуникация с {other_name}: {speaker_name}: {line}",
                })

        content = " ".join(summary_parts).strip()[:4000]
        if not content:
            continue

        topic_id = f"remote_t{turn_number}_{counterpart_id}"
        if topic_id not in dialogue_ids:
            dialogue.append({
                "topic_id": topic_id,
                "participants": [pov_id, counterpart_id],
                "mode": "remote",
                "summary": content,
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
            if records:
                total += len(records)
                row["user_input"] = _redact_text(str(row.get("user_input") or ""), records)
                if "scene_output" in row:
                    row["scene_output"] = _redact_text(str(row.get("scene_output") or ""), records)
                if "scene_tail" in row:
                    row["scene_tail"] = _redact_text(str(row.get("scene_tail") or ""), records)
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
