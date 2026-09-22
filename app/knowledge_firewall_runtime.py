from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import character_chunk_read, session_runtime, storage, writer_first_runtime
from .scene_compaction_runtime import active_memory_records
from .transactional_storage import session_transaction


_VERSION = 6
_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_ORIGINAL_CREATE_SESSION = None
_ORIGINAL_PARTICIPATION_BUNDLE = None

_DIRECT_SOURCE_KINDS = {
    "direct_observation",
    "heard",
    "read",
    "received",
    "told",
    "user_input",
}
_ALLOWED_SOURCE_KINDS = _DIRECT_SOURCE_KINDS | {"inference"}
_FORBIDDEN_SOURCE_KINDS = {
    "card",
    "character_card",
    "questionnaire",
    "chronology",
    "foundation",
    "lore",
    "scene_history",
    "recent_turns",
    "continuity_turns",
    "other_character_memory",
    "author_context",
    "future_guidance",
}

_SPEECH_RE = re.compile(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*[—-]\s*(?P<text>.*)$")
_NUMBER_RE = re.compile(r"(?<!\w)-?\d{2,4}(?!\w)")
_OPTION_RE = re.compile(r"(?m)^\s*([1-3])\.\s+(\S.*)$")
_OPTION_MARKERS = (
    ("Что я могу сделать:", "option_action"),
    ("Что я могу сказать:", "option_say"),
    ("Что я могу подумать:", "option_thought"),
)

_KNOWLEDGE_TOPIC_ROOTS = {
    "meeting_plan": ("встреч", "свидан", "визит", "назнач", "брони", "бронь", "планир"),
    "clothing": ("плать", "наряд", "одежд"),
    "promise": ("обещ", "договор"),
    "relationship_history": ("бывш", "родств", "брат", "сестр", "жених", "невест", "муж", "жена"),
    "secret": ("секрет", "тайн"),
}
_TEMPORAL_MARKERS = ("завтра", "послезавтра", "сегодня", "вечером", "утром", "ночью", "во сколько")
_CONTACT_RE = re.compile(r"(?iu)\\b(?:к|ко|с|со|у|от)\\s+([a-zа-яё][a-zа-яё-]{2,})")
_CONTACT_STOP = {
    "тебе", "тебя", "тобой", "нему", "него", "ним", "ней", "нее", "неё",
    "мне", "меня", "мной", "себе", "собой", "нами", "вами", "ними",
}


def _entity_stem(value: str) -> str:
    word = _norm(value).strip(".,!?;:()[]{}\"'«»")
    for ending in (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
        "ах", "ях", "ом", "ем", "ам", "ям", "ой", "ей", "ую", "юю",
        "ов", "ев", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(word) >= 5 and word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


def _contact_targets(text: str) -> set[str]:
    result: set[str] = set()
    for match in _CONTACT_RE.finditer(str(text or "")):
        raw = _norm(match.group(1))
        if raw in _CONTACT_STOP:
            continue
        stem = _entity_stem(raw)
        if len(stem) >= 3:
            result.add(stem)
    return result


def _knowledge_topics(text: str) -> set[str]:
    normalized = _norm(text)
    result: set[str] = set()
    for topic, roots in _KNOWLEDGE_TOPIC_ROOTS.items():
        if any(root in normalized for root in roots):
            result.add(topic)
    return result


def _has_temporal_marker(text: str) -> bool:
    normalized = _norm(text)
    return any(marker in normalized for marker in _TEMPORAL_MARKERS)


def _fact_free_sensitive_reason(text: str) -> str | None:
    topics = _knowledge_topics(text)
    if topics:
        return "topic:" + ",".join(sorted(topics))
    contacts = _contact_targets(text)
    if contacts and _has_temporal_marker(text):
        return "scheduled_contact:" + ",".join(sorted(contacts))
    if "во сколько" in _norm(text):
        return "schedule"
    if _NUMBER_RE.search(str(text or "")):
        return "numeric_literal"
    return None


def _source_supports_unit(unit_text: str, source_texts: List[str]) -> tuple[bool, Dict[str, Any]]:
    source_texts = [str(value or "") for value in source_texts if str(value or "").strip()]
    joined = "\n".join(source_texts)
    unit_topics = _knowledge_topics(unit_text)
    source_topics = _knowledge_topics(joined)
    missing_topics = sorted(unit_topics - source_topics)

    unit_contacts = _contact_targets(unit_text)
    source_contacts = _contact_targets(joined)
    scheduled_contacts = bool(unit_contacts and _has_temporal_marker(unit_text))
    missing_contacts: List[str] = []
    if scheduled_contacts:
        source_has_schedule = (
            _has_temporal_marker(joined)
            or bool(source_topics & {"meeting_plan"})
            or bool(_NUMBER_RE.search(joined))
        )
        if not source_has_schedule:
            missing_contacts = sorted(unit_contacts)
        else:
            missing_contacts = sorted(unit_contacts - source_contacts)

    missing_numbers: List[int] = []
    for match in _NUMBER_RE.finditer(str(unit_text or "")):
        try:
            number = int(match.group(0))
        except ValueError:
            continue
        if not any(_number_present(source, number) for source in source_texts):
            missing_numbers.append(number)

    ok = not missing_topics and not missing_contacts and not missing_numbers
    return ok, {
        "missing_topics": missing_topics,
        "missing_contact_targets": missing_contacts,
        "missing_numbers": missing_numbers,
    }


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _fact_text(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item or "")
    for key in ("fact", "text", "summary", "event", "description", "content", "memory", "note", "detail"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _fact_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("fact_id", "id"):
        if item.get(key):
            return str(item[key])
    return None


def _knowledge_only_bucket(bucket: Any) -> Dict[str, Any]:
    source = bucket if isinstance(bucket, dict) else {}
    result: Dict[str, Any] = {
        "knowledge": deepcopy(source.get("knowledge", [])) if isinstance(source.get("knowledge"), list) else [],
    }
    if isinstance(source.get("historical_knowledge_catalog"), list):
        result["historical_knowledge_catalog"] = deepcopy(source["historical_knowledge_catalog"])
    if source.get("older_history_available") not in (None, "", [], {}):
        result["older_history_available"] = deepcopy(source["older_history_available"])
    return result


def _firewall_contract() -> Dict[str, Any]:
    return {
        "version": _VERSION,
        "mandatory": True,
        "closed_world": True,
        "authoritative_prior_knowledge_path": "character_knowledge[character_id].knowledge",
        "current_turn_knowledge_path": "extracted.turn_knowledge",
        "exclusive_rule": (
            "Факт считается известным персонажу ТОЛЬКО если он есть в character_knowledge[ID].knowledge "
            "или сначала оформлен как turn_knowledge с реальным источником, существующим раньше использования. "
            "Анкета, card, chronology, recent/continuity turns, scene_history, foundation, lore, future_guidance, "
            "experience/dialogue memory и память другого персонажа НИКОГДА не дают фактическое знание."
        ),
        "current_turn_rule": (
            "Нельзя пользоваться сырым 'он увидел/услышал' как обходом. Сначала создай turn_knowledge: "
            "character_id, event_id, fact, source_kind, evidence. evidence должен дословно существовать в user_input "
            "или scene_output ДО реплики/мысли/действия, которое опирается на факт."
        ),
        "commit_ledger": {
            "knowledge_trace_complete": True,
            "knowledge_usage": (
                "Покрой каждую зарегистрированную реплику и каждый POV-вариант действия/реплики/мысли. "
                "fact_free=true допустим только для unit без внешнего фактического утверждения. "
                "Планы/встречи/одежда/обещания/секреты/конкретные числа и запланированный контакт требуют "
                "source_fact_ids/source_event_ids, а источник должен содержательно поддерживать именно этот claim."
            ),
            "durable_knowledge": (
                "knowledge_add разрешён только как долговечная копия уже валидного turn_knowledge; "
                "knowledge_add.source_event_id обязателен."
            ),
        },
        "forbidden_sources": sorted(_FORBIDDEN_SOURCE_KINDS),
    }


def _rewrite_packet(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base
        if (
            int(packet.get("strict_knowledge_firewall_version", 0) or 0) >= _VERSION
            and context
            and next(iter(context)) == "knowledge_firewall_v5"
            and isinstance(context.get("knowledge_firewall_v5"), dict)
            and int(context["knowledge_firewall_v5"].get("version", 0) or 0) >= _VERSION
        ):
            return base

        memory = context.get("character_memory") if isinstance(context.get("character_memory"), dict) else {}
        author_recollection: Dict[str, Any] = {}
        strict_memory: Dict[str, Any] = {}
        for character_id, bucket in memory.items():
            if not isinstance(bucket, dict):
                continue
            strict_memory[str(character_id)] = _knowledge_only_bucket(bucket)
            author_recollection[str(character_id)] = {
                "experiences": deepcopy(bucket.get("experiences", [])) if isinstance(bucket.get("experiences"), list) else [],
                "dialogue_memory": deepcopy(bucket.get("dialogue_memory", [])) if isinstance(bucket.get("dialogue_memory"), list) else [],
                "fact_authority": False,
            }

        context["character_memory"] = strict_memory
        context["character_knowledge"] = deepcopy(strict_memory)
        context["author_only_recollection_context"] = author_recollection
        context.pop("knowledge_firewall_v5", None)
        context = {"knowledge_firewall_v5": _firewall_contract(), **context}

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[index:index + size] for index in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["strict_knowledge_firewall_version"] = _VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "strict_knowledge_firewall_version": _VERSION,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _character_alias_map(cards: List[Dict[str, Any]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        result[_norm(cid)] = cid
        for value in storage._card_names(card):
            result[_norm(value)] = cid
    return result


def _option_units(text: str, pov_id: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    positions = []
    for marker, kind in _OPTION_MARKERS:
        positions.append((text.find(marker), marker, kind))
    found = [(pos, marker, kind) for pos, marker, kind in positions if pos >= 0]
    found.sort(key=lambda item: item[0])
    for index, (start, marker, kind) in enumerate(found):
        end = found[index + 1][0] if index + 1 < len(found) else len(text)
        section = text[start + len(marker):end]
        for match in _OPTION_RE.finditer(section):
            number = int(match.group(1))
            result.append({
                "unit_id": f"{kind}:{number}",
                "character_id": pov_id,
                "text": match.group(2).strip(),
                "position": start + len(marker) + match.start(),
            })
    return result


def _knowledge_units(scene_output: str, cards: List[Dict[str, Any]], pov_id: str) -> List[Dict[str, Any]]:
    aliases = _character_alias_map(cards)
    result: List[Dict[str, Any]] = []
    speech_index = 0
    for match in _SPEECH_RE.finditer(scene_output):
        cid = aliases.get(_norm(match.group("speaker")))
        if not cid:
            continue
        speech_index += 1
        result.append({
            "unit_id": f"speech:{speech_index}",
            "character_id": cid,
            "text": match.group("text").strip(),
            "position": match.start(),
        })
    result.extend(_option_units(scene_output, pov_id))
    result.sort(key=lambda row: (int(row["position"]), str(row["unit_id"])))
    return result


def _persistent_knowledge(root, character_id: str) -> List[Dict[str, Any]]:
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    bucket = buckets.get(character_id, {}) if isinstance(buckets.get(character_id), dict) else {}
    return active_memory_records(bucket.get("knowledge", []))


def _source_evidence_position(event: Dict[str, Any], user_input: str, scene_output: str) -> int:
    evidence = str(event.get("evidence") or "").strip()
    if not evidence:
        return -10**9
    if evidence in user_input:
        return -1
    return scene_output.find(evidence)


def _validate_turn_knowledge(
    extracted: Dict[str, Any],
    *,
    root,
    user_input: str,
    scene_output: str,
    valid_character_ids: set[str],
) -> Dict[str, Dict[str, Any]]:
    rows = extracted.get("turn_knowledge")
    if not isinstance(rows, list):
        raise HTTPException(
            status_code=409,
            detail={"code": "TURN_KNOWLEDGE_LEDGER_REQUIRED", "instruction": "Передай extracted.turn_knowledge массивом, даже если он пуст."},
        )

    result: Dict[str, Dict[str, Any]] = {}
    persistent_ids: Dict[str, set[str]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=409, detail={"code": "TURN_KNOWLEDGE_INVALID"})
        event_id = str(raw.get("event_id") or "").strip()
        character_id = str(raw.get("character_id") or "").strip()
        fact = str(raw.get("fact") or "").strip()
        source_kind = str(raw.get("source_kind") or "").casefold().strip()
        if not event_id or event_id in result or not character_id or not fact:
            raise HTTPException(status_code=409, detail={"code": "TURN_KNOWLEDGE_INVALID"})
        if character_id not in valid_character_ids:
            raise HTTPException(status_code=409, detail={"code": "TURN_KNOWLEDGE_UNKNOWN_CHARACTER", "character_id": character_id})
        if source_kind in _FORBIDDEN_SOURCE_KINDS or source_kind not in _ALLOWED_SOURCE_KINDS:
            raise HTTPException(
                status_code=409,
                detail={"code": "TURN_KNOWLEDGE_FORBIDDEN_SOURCE", "event_id": event_id, "source_kind": source_kind},
            )

        event = deepcopy(raw)
        if source_kind in _DIRECT_SOURCE_KINDS:
            evidence = str(event.get("evidence") or "").strip()
            pos = _source_evidence_position(event, user_input, scene_output)
            if not evidence or pos == -1 and evidence not in user_input or pos < 0 and evidence not in user_input:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TURN_KNOWLEDGE_EVIDENCE_MISSING", "event_id": event_id},
                )
            event["_evidence_position"] = pos
        else:
            source_fact_ids = [str(value) for value in event.get("source_fact_ids", []) if str(value)]
            if not source_fact_ids:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TURN_KNOWLEDGE_INFERENCE_SOURCE_REQUIRED", "event_id": event_id},
                )
            if character_id not in persistent_ids:
                persistent_ids[character_id] = {
                    str(_fact_id(item))
                    for item in _persistent_knowledge(root, character_id)
                    if _fact_id(item)
                }
            unknown = [value for value in source_fact_ids if value not in persistent_ids[character_id]]
            if unknown:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TURN_KNOWLEDGE_INFERENCE_SOURCE_UNKNOWN", "event_id": event_id, "unknown_fact_ids": unknown},
                )
            event["_evidence_position"] = -1
        result[event_id] = event
    return result


def _author_numbers(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, bool) or value is None:
        return result
    if isinstance(value, (int, float)):
        number = int(value)
        if float(value) == float(number) and 10 <= abs(number) <= 9999:
            result.add(number)
        return result
    if isinstance(value, str):
        for match in _NUMBER_RE.finditer(value):
            try:
                number = int(match.group(0))
            except ValueError:
                continue
            if 10 <= abs(number) <= 9999:
                result.add(number)
        return result
    if isinstance(value, dict):
        for item in value.values():
            result.update(_author_numbers(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_author_numbers(item))
    return result


_ONES = {
    0: "ноль", 1: "один", 2: "два", 3: "три", 4: "четыре", 5: "пять",
    6: "шесть", 7: "семь", 8: "восемь", 9: "девять", 10: "десять",
    11: "одиннадцать", 12: "двенадцать", 13: "тринадцать", 14: "четырнадцать",
    15: "пятнадцать", 16: "шестнадцать", 17: "семнадцать", 18: "восемнадцать", 19: "девятнадцать",
}
_TENS = {20: "двадцать", 30: "тридцать", 40: "сорок", 50: "пятьдесят", 60: "шестьдесят", 70: "семьдесят", 80: "восемьдесят", 90: "девяносто"}
_HUNDREDS = {100: "сто", 200: "двести", 300: "триста", 400: "четыреста", 500: "пятьсот", 600: "шестьсот", 700: "семьсот", 800: "восемьсот", 900: "девятьсот"}


def _ru_number(value: int) -> str | None:
    if value < 0 or value > 999:
        return None
    if value < 20:
        return _ONES[value]
    parts: List[str] = []
    hundreds = value // 100 * 100
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
        value %= 100
    if value < 20:
        if value:
            parts.append(_ONES[value])
    else:
        tens = value // 10 * 10
        parts.append(_TENS[tens])
        value %= 10
        if value:
            parts.append(_ONES[value])
    return " ".join(parts)


def _number_present(text: str, number: int) -> bool:
    if re.search(rf"(?<!\w){re.escape(str(number))}(?!\w)", text):
        return True
    word = _ru_number(number)
    return bool(word and _norm(word) in _norm(text))


def _authorized_corpus(
    root,
    character_id: str,
    turn_events: Dict[str, Dict[str, Any]],
    *,
    user_input: str,
    pov_id: str,
) -> str:
    parts = [_fact_text(item) for item in _persistent_knowledge(root, character_id)]
    parts.extend(str(event.get("fact") or "") for event in turn_events.values() if str(event.get("character_id")) == character_id)
    if character_id == pov_id:
        parts.append(user_input)
    return "\n".join(part for part in parts if part)


def _validate_literal_leaks(
    *,
    root,
    units: List[Dict[str, Any]],
    turn_events: Dict[str, Dict[str, Any]],
    user_input: str,
    pov_id: str,
) -> None:
    source = storage._read_json(root / "source.json", {})
    chronology = storage._read_json(root / "chronology.json", [])
    cards = storage._load_cards(root, source)
    author_numbers = _author_numbers({
        "characters": cards,
        "foundation": source.get("foundation"),
        "lore": source.get("lore"),
        "hidden_lore": source.get("hidden_lore"),
        "world": source.get("world"),
        "chronology": chronology,
    })
    if not author_numbers:
        return

    corpus_cache: Dict[str, str] = {}
    for unit in units:
        character_id = str(unit.get("character_id") or "")
        text = str(unit.get("text") or "")
        if not character_id or not text:
            continue
        corpus = corpus_cache.setdefault(
            character_id,
            _authorized_corpus(root, character_id, turn_events, user_input=user_input, pov_id=pov_id),
        )
        for number in author_numbers:
            if _number_present(text, number) and not _number_present(corpus, number):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "KNOWLEDGE_LITERAL_LEAK",
                        "character_id": character_id,
                        "unit_id": unit.get("unit_id"),
                        "literal": number,
                        "instruction": (
                            "Числовой факт существует в авторском каноне, но отсутствует в знаниях этого персонажа. "
                            "Убери его из реплики/мысли/действия либо сначала дай персонажу реальный turn_knowledge источник ДО использования."
                        ),
                    },
                )


def _validate_usage_ledger(
    extracted: Dict[str, Any],
    *,
    root,
    units: List[Dict[str, Any]],
    turn_events: Dict[str, Dict[str, Any]],
) -> None:
    if extracted.get("knowledge_trace_complete") is not True:
        raise HTTPException(
            status_code=409,
            detail={"code": "KNOWLEDGE_TRACE_REQUIRED", "instruction": "Перед commit поставь extracted.knowledge_trace_complete=true после полного покрытия knowledge-sensitive units."},
        )
    usage = extracted.get("knowledge_usage")
    if not isinstance(usage, list):
        raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_USAGE_REQUIRED"})

    expected = {str(unit["unit_id"]): unit for unit in units}
    provided: Dict[str, Dict[str, Any]] = {}
    for raw in usage:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_USAGE_INVALID"})
        unit_id = str(raw.get("unit_id") or "").strip()
        if not unit_id or unit_id in provided:
            raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_USAGE_INVALID", "unit_id": unit_id})
        provided[unit_id] = raw

    missing = [unit_id for unit_id in expected if unit_id not in provided]
    extra = [unit_id for unit_id in provided if unit_id not in expected]
    if missing or extra:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KNOWLEDGE_USAGE_COVERAGE_MISMATCH",
                "missing_unit_ids": missing,
                "unexpected_unit_ids": extra,
                "expected_units": [
                    {"unit_id": unit["unit_id"], "character_id": unit["character_id"], "text": unit["text"][:180]}
                    for unit in units
                ],
            },
        )

    persistent_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for unit_id, unit in expected.items():
        row = provided[unit_id]
        character_id = str(unit["character_id"])
        if str(row.get("character_id") or "") != character_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "KNOWLEDGE_USAGE_CHARACTER_MISMATCH", "unit_id": unit_id, "expected_character_id": character_id},
            )

        source_fact_ids = [str(value) for value in row.get("source_fact_ids", []) if str(value)]
        source_event_ids = [str(value) for value in row.get("source_event_ids", []) if str(value)]
        fact_free = row.get("fact_free") is True
        if fact_free and (source_fact_ids or source_event_ids):
            raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_USAGE_INVALID", "unit_id": unit_id})
        if fact_free:
            sensitive_reason = _fact_free_sensitive_reason(str(unit.get("text") or ""))
            if sensitive_reason:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "KNOWLEDGE_FACT_FREE_SENSITIVE",
                        "unit_id": unit_id,
                        "character_id": character_id,
                        "reason": sensitive_reason,
                        "instruction": (
                            "Эта реплика/мысль содержит конкретный фактический claim и не может быть fact_free. "
                            "Укажи реальный knowledge source этого персонажа либо перепиши unit без неподтверждённого факта."
                        ),
                    },
                )
        if not fact_free and not (source_fact_ids or source_event_ids):
            raise HTTPException(
                status_code=409,
                detail={"code": "KNOWLEDGE_USAGE_SOURCE_REQUIRED", "unit_id": unit_id},
            )

        if character_id not in persistent_cache:
            persistent_cache[character_id] = {
                str(_fact_id(item)): item
                for item in _persistent_knowledge(root, character_id)
                if _fact_id(item)
            }
        unknown_facts = [value for value in source_fact_ids if value not in persistent_cache[character_id]]
        if unknown_facts:
            raise HTTPException(
                status_code=409,
                detail={"code": "KNOWLEDGE_USAGE_UNKNOWN_FACT", "unit_id": unit_id, "unknown_fact_ids": unknown_facts},
            )

        source_texts = [
            _fact_text(persistent_cache[character_id][fact_id])
            for fact_id in source_fact_ids
            if fact_id in persistent_cache[character_id]
        ]
        for event_id in source_event_ids:
            event = turn_events.get(event_id)
            if event is None:
                forbidden_prefix = str(event_id).casefold().startswith(
                    ("dialogue_", "dialogue_t", "exp_", "experience_", "chrono_", "scene_")
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "KNOWLEDGE_USAGE_FORBIDDEN_SOURCE" if forbidden_prefix else "KNOWLEDGE_USAGE_UNKNOWN_EVENT",
                        "unit_id": unit_id,
                        "event_id": event_id,
                        "instruction": (
                            "dialogue_memory/experiences/chronology/scene IDs не являются источниками знания. "
                            "source_event_ids могут ссылаться только на валидный turn_knowledge текущего хода."
                            if forbidden_prefix
                            else "Источник знания не существует в валидном turn_knowledge текущего хода."
                        ),
                    },
                )
            if str(event.get("character_id") or "") != character_id:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "KNOWLEDGE_USAGE_FOREIGN_EVENT", "unit_id": unit_id, "event_id": event_id},
                )
            if int(event.get("_evidence_position", -1)) >= int(unit.get("position", 0)):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "KNOWLEDGE_SOURCE_AFTER_USE",
                        "unit_id": unit_id,
                        "event_id": event_id,
                        "instruction": "Источник появился после использования факта. Перепиши сцену; задним числом источник не засчитывается.",
                    },
                )
            source_texts.append(str(event.get("fact") or ""))

        if not fact_free:
            relevant, relevance = _source_supports_unit(str(unit.get("text") or ""), source_texts)
            if not relevant:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "KNOWLEDGE_SOURCE_NOT_RELEVANT",
                        "unit_id": unit_id,
                        "character_id": character_id,
                        **relevance,
                        "instruction": (
                            "Указанный knowledge source существует, но не поддерживает фактическое содержание unit. "
                            "Нельзя прикрывать новый факт старой записью с тем же именем/темой. "
                            "Укажи источник именно этого факта либо перепиши сцену."
                        ),
                    },
                )


def _validate_knowledge_add(extracted: Dict[str, Any], turn_events: Dict[str, Dict[str, Any]]) -> None:
    rows = extracted.get("knowledge_add")
    if not isinstance(rows, list):
        return
    for raw in rows:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=409, detail={"code": "KNOWLEDGE_ADD_INVALID"})
        fact_id = str(raw.get("fact_id") or "").strip()
        character_id = str(raw.get("character_id") or "").strip()
        source_event_id = str(raw.get("source_event_id") or "").strip()
        if not fact_id or not character_id or not source_event_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "KNOWLEDGE_ADD_SOURCE_REQUIRED",
                    "instruction": "Для strict packet каждый knowledge_add обязан иметь fact_id, character_id и source_event_id валидного turn_knowledge.",
                },
            )
        event = turn_events.get(source_event_id)
        if event is None or str(event.get("character_id") or "") != character_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "KNOWLEDGE_ADD_SOURCE_INVALID", "fact_id": fact_id, "source_event_id": source_event_id},
            )


def _validate_knowledge_commit(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    packet = storage._read_json(root / "turn_packet.json", {})
    if (
        not isinstance(packet, dict)
        or int(packet.get("strict_knowledge_firewall_version", 0) or 0) < _VERSION
        or not bool(packet.get("strict_knowledge_capable"))
    ):
        return

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    if extracted.get("knowledge_reviewed") is not True:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KNOWLEDGE_REVIEW_REQUIRED",
                "packet_id": packet.get("packet_id"),
                "instruction": (
                    "Проверь знания POV и всех NPC в scene_output. Прошлый факт допустим только из knowledge этого персонажа; "
                    "новый факт текущего хода сначала оформи turn_knowledge с реальным источником ДО использования."
                ),
            },
        )

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    valid_character_ids = {storage._card_id(card) for card in cards if storage._card_id(card)}

    scene_output = str(payload.get("scene_output") or "")
    user_input = str(payload.get("user_input") or "")
    units = _knowledge_units(scene_output, cards, pov_id)
    turn_events = _validate_turn_knowledge(
        extracted,
        root=root,
        user_input=user_input,
        scene_output=scene_output,
        valid_character_ids=valid_character_ids,
    )
    _validate_usage_ledger(extracted, root=root, units=units, turn_events=turn_events)
    _validate_literal_leaks(
        root=root,
        units=units,
        turn_events=turn_events,
        user_input=user_input,
        pov_id=pov_id,
    )
    _validate_knowledge_add(extracted, turn_events)


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_knowledge_commit(session_id, payload)
    return _ORIGINAL_COMMIT(session_id, payload)


def _initial_knowledge_rows(section: Any) -> Iterable[tuple[str, Any]]:
    if not isinstance(section, dict):
        return []
    characters = section.get("characters", section)
    if isinstance(characters, list):
        result = []
        for row in characters:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("character_id") or row.get("id") or "").strip()
            values = row.get("knowledge", [])
            if cid and isinstance(values, list):
                result.extend((cid, value) for value in values)
        return result
    if isinstance(characters, dict):
        result = []
        for cid, bucket in characters.items():
            values = bucket.get("knowledge", []) if isinstance(bucket, dict) else bucket
            if isinstance(values, list):
                result.extend((str(cid), value) for value in values)
        return result
    return []


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")[:60] or "character"


def _seed_initial_knowledge(session_id: str, novel: Dict[str, Any]) -> None:
    section = novel.get("knowledge")
    if not isinstance(section, dict):
        return
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        return
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    changed = False
    counters: Dict[str, int] = {}
    for character_id, raw in _initial_knowledge_rows(section):
        if isinstance(raw, str):
            record: Dict[str, Any] = {"fact": raw}
        elif isinstance(raw, dict):
            record = deepcopy(raw)
        else:
            continue
        fact = str(record.get("fact") or record.get("text") or "").strip()
        if not fact:
            continue
        counters[character_id] = counters.get(character_id, 0) + 1
        record["character_id"] = character_id
        record["fact"] = fact
        record.setdefault("fact_id", f"initial_{_safe_id(character_id)}_{counters[character_id]}")
        record.setdefault("learned_turn", 0)
        record.setdefault("confidence", "certain")
        record.setdefault("source_kind", "initial_knowledge")
        bucket = storage._memory_bucket(memory, character_id)
        before = json.dumps(bucket.get("knowledge", []), ensure_ascii=False, sort_keys=True)
        storage._upsert_by_id(bucket["knowledge"], record, "fact_id")
        after = json.dumps(bucket.get("knowledge", []), ensure_ascii=False, sort_keys=True)
        changed = changed or before != after
    if changed:
        storage._write_json(root / "memory.json", memory)


def _create_session(novel: Dict[str, Any], *, session_id: str | None = None, meta_patch: Dict[str, Any] | None = None) -> Dict[str, Any]:
    result = dict(_ORIGINAL_CREATE_SESSION(novel, session_id=session_id, meta_patch=meta_patch))
    sid = str(result.get("session_id") or session_id or "")
    if sid:
        _seed_initial_knowledge(sid, novel)
    return result


def _strict_participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    bundle = dict(_ORIGINAL_PARTICIPATION_BUNDLE(session_id, character_id))
    memory = deepcopy(bundle.get("personal_memory", {})) if isinstance(bundle.get("personal_memory"), dict) else {}
    bundle["personal_memory"] = memory
    bundle["character_knowledge"] = {
        "path": "personal_memory.knowledge",
        "fact_authority": True,
    }
    bundle["author_only_recollection_context"] = {
        "experiences_path": "personal_memory.experiences",
        "dialogue_memory_path": "personal_memory.dialogue_memory",
        "fact_authority": False,
    }
    firewall = bundle.get("knowledge_firewall") if isinstance(bundle.get("knowledge_firewall"), dict) else {}
    firewall.update({
        "version": _VERSION,
        "closed_world": True,
        "authoritative_prior_knowledge_path": "personal_memory.knowledge",
        "card_is_author_only": True,
        "experiences_are_not_fact_authority": True,
        "dialogue_memory_is_not_fact_authority": True,
        "instruction": "CARD и recollection context — авторский материал. Фактическое знание только character_knowledge.knowledge.",
    })
    bundle["knowledge_firewall"] = firewall
    bundle["instruction"] = (
        "Фактическое знание только personal_memory.knowledge; character_knowledge.path указывает на него. "
        "Card, chronology, experiences/dialogue_memory и любой другой author context не дают новых фактов."
    )
    return bundle


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT, _ORIGINAL_CREATE_SESSION, _ORIGINAL_PARTICIPATION_BUNDLE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    _ORIGINAL_CREATE_SESSION = storage.create_session
    _ORIGINAL_PARTICIPATION_BUNDLE = character_chunk_read._participation_bundle
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
    storage.create_session = _create_session
    character_chunk_read._participation_bundle = _strict_participation_bundle
