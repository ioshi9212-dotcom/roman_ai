from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from fastapi import HTTPException

from . import character_chunk_read, npc_relationship_runtime, relationship_runtime, session_runtime, storage, writer_first_runtime
from .profile_templates import (
    normalize_character_profile,
    render_character_profile,
    render_hidden_lore,
    render_knowledge_journal,
    render_novel_profile,
)
from .transactional_storage import session_transaction


_PROFILE_RUNTIME_VERSION = 4
_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_ORIGINAL_PARTICIPATION_BUNDLE = None


def _simple_session(root) -> bool:
    source = storage._read_json(root / "source.json", {})
    try:
        if int(source.get("version", 1) or 1) >= 5:
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(source.get("profile_schema"), dict)


def _profile_map(cards: List[Dict[str, Any]], ids: List[str]) -> Dict[str, str]:
    wanted = set(ids)
    result: Dict[str, str] = {}
    for card in cards:
        cid = storage._card_id(card)
        if cid in wanted:
            result[cid] = render_character_profile(card)
    return result


def _speaker_context(ids: List[str], pov_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for cid in ids:
        result[cid] = {
            "character_id": cid,
            "profile_path": f"character_profiles[{cid}]",
            "knowledge_source": {
                "kind": "packet_character_memory",
                "character_id": cid,
                "complete": True,
            },
            "current_perception": "only what this character can see/hear/receive in the current scene",
            "relationship_path": f"relationship_lens.relations_in_current_scene[owner_character_id={cid}]",
            "rule": (
                "Реплики, мысли и решения строятся отдельно: self-known части своего profile, свой knowledge_journal, "
                "доступное текущее восприятие и отношение к POV. Ветки profile с unknown_to_self/hidden_from_self/"
                "not_known_to_self/known_to_self=false/author_only недоступны самому персонажу. "
                "Приватный POV-контекст не источник; явная коммуникация внутри ( ) доступна только указанному получателю."
            ),
            "is_pov": cid == pov_id,
        }
    return result


def _rewrite_packet(session_id: str, base: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not _simple_session(root):
        return base

    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base

        source = storage._read_json(root / "source.json", {})
        cards = storage._load_cards(root, source)
        state = storage._read_json(root / "state.json", {})
        pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
        pov_id = str(pov.get("character_id") or "")

        ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
        if pov_id and pov_id not in ids:
            ids.insert(0, pov_id)
        if not ids:
            ids = storage._present_character_ids(state)

        profiles = _profile_map(cards, ids)

        for key in (
            "knowledge_firewall_v5",
            "dialogue_policy",
            "dialogue_frames",
            "author_only_recollection_context",
            "knowledge_guard",
            "character_memory",
            "character_cards",
            "scene_characters",
            "character_knowledge",
            "knowledge_journals",
        ):
            context.pop(key, None)

        author = context.get("author_context")
        if isinstance(author, dict):
            author = deepcopy(author)
            for key in ("character_cards", "characters", "memory", "character_memory"):
                author.pop(key, None)
            context["author_context"] = author

        scene_presence = context.get("scene_presence")
        if isinstance(scene_presence, dict):
            scene_presence = deepcopy(scene_presence)
            roster = scene_presence.get("roster")
            if isinstance(roster, list):
                for row in roster:
                    if not isinstance(row, dict) or not row.get("character_id"):
                        continue
                    cid = str(row["character_id"])
                    row["full_card_path"] = f"character_profiles[{cid}]"
                    row.pop("memory_path", None)
                    row["knowledge_source"] = "mandatory_complete_knowledge_read"
            context["scene_presence"] = scene_presence

        context["novel_profile"] = render_novel_profile(source.get("novel", {}))
        context["character_profiles"] = profiles
        context["speaker_context"] = _speaker_context(ids, pov_id)
        context["director_only"] = {
            "hidden_lore": render_hidden_lore(source.get("hidden_lore", {})),
            "chronology_rule": (
                "Chronology/scene history are objective continuity for directing the world only. "
                "They never become POV or NPC knowledge by themselves."
            ),
        }
        living = context.get("living_world")
        if isinstance(living, dict):
            living = deepcopy(living)
            frames = living.get("npc_actor_frames")
            if isinstance(frames, list):
                for frame in frames:
                    if not isinstance(frame, dict):
                        continue
                    cid = str(frame.get("character_id") or "")
                    frame.pop("memory_path", None)
                    frame["knowledge_source"] = "mandatory_complete_knowledge_read"
                    frame["instruction"] = (
                        "Поведение: character_drivers + отношения + intents. Фактическое знание: собственный profile, "
                        "полностью прочитанный knowledge-read и текущее восприятие."
                    )
            context["living_world"] = living

        guards = context.get("scene_logic_guardrails")
        if isinstance(guards, dict):
            guards = deepcopy(guards)

            causality = guards.get("knowledge_causality")
            causality = deepcopy(causality) if isinstance(causality, dict) else {}
            causality.update({
                "mandatory": True,
                "applies_to": "speech, POV narration and POV inner view",
                "source_before_use": True,
                "no_retroactive_justification": True,
                "character_knowledge_is_closed_world": True,
                "allowed_sources": [
                    "speaker_context[character_id].profile_path for self-known profile facts",
                    "completed mandatory full knowledge read for this character",
                    "current perception physically or communicatively available to this character",
                    "earlier current-turn public speech or communication explicitly addressed to this character",
                ],
                "author_only_not_character_knowledge": [
                    "another character's profile or knowledge_journal",
                    "chronology/recent_turns/continuity_turns/scene_history",
                    "director_only hidden_lore and author context",
                    "foundation/future_guidance/lore/world canon/location_context/canon_notes_context",
                    "POV parenthetical text except observable physical effects or communication explicitly addressed to this character",
                    "another character's private information",
                ],
                "rule": (
                    "Для каждого персонажа источник факта должен существовать ДО его реплики/вывода/осмысленного действия. "
                    "NPC: собственный profile + собственный knowledge_journal + доступное текущее восприятие/коммуникация. "
                    "POV: собственный profile + journal + доступное восприятие. Нельзя оправдывать знание задним числом; "
                    "chronology, hidden_lore, location_context, canon_notes_context, другие profiles/journals и director-only не являются знанием персонажа."
                ),
            })
            guards["knowledge_causality"] = causality

            review = guards.get("knowledge_review")
            review = deepcopy(review) if isinstance(review, dict) else {}
            review.update({
                "mandatory": True,
                "applies_to": "real speech, factual POV narration and factual character conclusions",
                "rule": (
                    "Перед commit проверь каждого говорящего/знающего персонажа отдельно по speaker_context. "
                    "Если источника не было до использования, перепиши конкретную реплику/вывод. "
                    "V5 не требует model-supplied fact/source ledger, но причинность знания остаётся обязательной."
                ),
            })
            guards["knowledge_review"] = review
            context["scene_logic_guardrails"] = guards

        context["simple_knowledge_rules"] = {
            "version": _PROFILE_RUNTIME_VERSION,
            "npc": (
                "Для каждого NPC отдельно: собственный profile + knowledge_journal + доступное восприятие + отношения. "
                "Приватные ( ) не дают знания; явно адресованное внутри ( ) получает только адресат."
            ),
            "self_knowledge": (
                "Собственный profile является самознанием персонажа. Если бытовой self-факт реально отсутствует "
                "и нужен сцене, его можно придумать непротиворечиво и сразу сохранить через character_upserts "
                "в соответствующее поле фиксированного профиля. В knowledge_journal собственную анкету не дублировать."
            ),
            "pov_narration": (
                "Режиссура и внутренний взгляд POV используют только profile POV, knowledge_journal POV и то, "
                "что POV сейчас видит, слышит, чувствует или уже лично узнала. Не выдавать hidden_lore, chronology "
                "или чужой profile как знание POV."
            ),
            "pov_ai_speech": (
                "ИИ может самостоятельно писать за POV только обычные бытовые и низкорисковые реплики. "
                "Не раскрывать за игрока личные сведения, тайны, признания, обещания, согласие/отказ, позицию в конфликте "
                "или информацию, способную заметно изменить сюжет или отношения."
            ),
            "location_context": (
                "location_context — физический канон текущего места для режиссуры, не личное знание персонажа автоматически. "
                "Связанный offscreen персонаж требует bundle до участия."
            ),
            "canon_notes_context": (
                "canon_notes_context — только релевантные устойчивые авторские факты; они не становятся личным знанием без реального источника."
            ),
            "knowledge_transport": (
                "knowledge_journal не дублируется в writer packet. Для каждого участника он читается полностью только "
                "через prepareCharacterKnowledgeRead + все getCharacterKnowledgeChunk; commit требует complete read."
            ),
            "learned_facts": (
                "Новые знания о других и мире сохраняй простыми записями knowledge_journal_add: "
                "{character_id, date?, period?, text}. Никаких fact_id/source_fact_ids/source_unit_id."
            ),
        }

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["simple_profile_runtime_version"] = _PROFILE_RUNTIME_VERSION
        packet.pop("strict_knowledge_firewall_version", None)
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
            "simple_profile_runtime": True,
            "strict_knowledge_firewall_version": None,
        })
        return result


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _current_date_period(state: Dict[str, Any]) -> tuple[Any, Any]:
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    date = current.get("date") or current.get("game_date") or current.get("calendar_date")
    period = current.get("period") or current.get("time_of_day")
    if period in (None, ""):
        time = str(current.get("time") or current.get("game_time") or "")
        try:
            hour = int(time.split(":", 1)[0])
        except (TypeError, ValueError):
            hour = -1
        if 5 <= hour <= 11:
            period = "утро"
        elif 12 <= hour <= 16:
            period = "день"
        elif 17 <= hour <= 21:
            period = "вечер"
        elif hour >= 0:
            period = "ночь"
    return date, period


def _normalize_upserts(root, extracted: Dict[str, Any]) -> None:
    values = extracted.get("character_upserts")
    if not isinstance(values, list):
        return
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    by_id = {storage._card_id(card): card for card in cards if storage._card_id(card)}
    normalized: List[Dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, dict):
            continue
        cid = storage._card_id(raw)
        if not cid:
            continue
        if cid in by_id:
            merged = storage._deep_merge(by_id[cid], raw)
            normalized.append(normalize_character_profile(merged))
        else:
            normalized.append(normalize_character_profile(raw))
    extracted["character_upserts"] = normalized


def _prepare_journal_entries(root, extracted: Dict[str, Any]) -> None:
    rows = extracted.get("knowledge_journal_add")
    if not isinstance(rows, list):
        extracted["knowledge_journal_add"] = []
        return
    state = storage._read_json(root / "state.json", {})
    date, period = _current_date_period(state)
    clean: List[Dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        character_id = raw.get("character_id")
        text = str(raw.get("text") or raw.get("fact") or raw.get("summary") or "").strip()
        if not character_id or not text:
            continue
        clean.append({
            "character_id": str(character_id),
            "date": raw.get("date") or date,
            "period": raw.get("period") or period,
            "text": text,
        })
    extracted["knowledge_journal_add"] = clean



_SPEECH_RE = re.compile(r"(?m)^\s*\*\*(?P<speaker>[^*\n]+)\*\*\s*[—-]\s*(?P<text>.*)$")
_COMMUNICATION_RE = re.compile(
    r"(?iu)\b(?:написать|ответить|отправить|переслать|сказать|сообщить|шепнуть|показать|позвонить)\s+([^\s,.;:()—-]+)"
)
_CHAT_RE = re.compile(
    r"(?iu)\b(?:переписк\w*|чат\w*)\s+(?:с|для)\s+([^\s,.;:()—-]+)"
)
_PRIVATE_ACTION_PREFIXES = {
    "встать", "сесть", "подойти", "отойти", "пойти", "уйти", "вернуться", "взять", "достать",
    "убрать", "положить", "открыть", "закрыть", "посмотреть", "повернуть", "поднять", "опустить",
    "схватить", "обнять", "поцеловать", "погладить", "залезть", "выйти", "зайти", "пройти",
    "наклониться", "присесть", "лечь", "встать", "сместить", "перехватить", "рассмотреть",
    "закатить", "улыбнуться", "усмехнуться", "отвернуться", "продолжить", "остаться",
}
_PRIVATE_STOP_WORDS = {
    "который", "которая", "которое", "которые", "чтобы", "потом", "сейчас", "теперь", "снова",
    "вообще", "просто", "только", "очень", "себе", "тебе", "тебя", "меня", "мне", "него", "нему",
    "него", "ней", "нее", "него", "этот", "эта", "это", "того", "там", "тут", "сюда", "туда",
    "сказать", "написать", "ответить", "отправить", "показать", "переслать", "сообщить",
    "реакция", "вопрос", "ответ", "смотреть", "посмотреть", "думать", "подумать",
}


def _privacy_norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _privacy_stem(value: str) -> str:
    word = _privacy_norm(value).strip(".,!?;:()[]{}\"'«»")
    for ending in (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
        "ах", "ях", "ом", "ем", "ам", "ям", "ой", "ей", "ую", "юю",
        "ов", "ев", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(word) >= 5 and word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[:-len(ending)]
    return word


def _privacy_terms(text: str) -> set[str]:
    result: set[str] = set()
    for match in re.finditer(r"(?iu)[a-zа-яё][a-zа-яё-]{3,}", str(text or "")):
        raw = _privacy_norm(match.group(0))
        stem = _privacy_stem(raw)
        if raw in _PRIVATE_STOP_WORDS or stem in {_privacy_stem(value) for value in _PRIVATE_STOP_WORDS}:
            continue
        if stem:
            result.add(stem)
    return result


def _alias_maps(cards: List[Dict[str, Any]]) -> tuple[Dict[str, str], Dict[str, str]]:
    exact: Dict[str, str] = {}
    stems: Dict[str, str] = {}
    for card in cards:
        cid = storage._card_id(card)
        if not cid:
            continue
        exact[_privacy_norm(cid)] = cid
        stems[_privacy_stem(cid)] = cid
        for value in storage._card_names(card):
            norm = _privacy_norm(value)
            if not norm:
                continue
            exact[norm] = cid
            first = norm.split()[0]
            exact[first] = cid
            stems[_privacy_stem(first)] = cid
    return exact, stems


def _resolve_recipient(token: str, exact: Dict[str, str], stems: Dict[str, str]) -> str | None:
    norm = _privacy_norm(token)
    if norm in exact:
        return exact[norm]
    return stems.get(_privacy_stem(norm))


def _looks_like_action(sentence: str) -> bool:
    words = re.findall(r"(?iu)[a-zа-яё][a-zа-яё-]+", str(sentence or ""))[:4]
    if not words:
        return False
    roots = {_privacy_stem(value) for value in _PRIVATE_ACTION_PREFIXES}
    return any(_privacy_stem(word) in roots for word in words[:2])


def _communication_payload(stage_text: str, match_end: int) -> str:
    tail = str(stage_text or "")[match_end:].lstrip(" \t,:")
    if tail.startswith("-") or tail.startswith("—"):
        tail = tail[1:].strip()
    if not tail:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", tail)
    kept: List[str] = []
    for index, part in enumerate(parts):
        clean = part.strip()
        if not clean:
            continue
        if index > 0 and _looks_like_action(clean):
            break
        kept.append(clean)
    return " ".join(kept).strip()


def _private_input_access(user_input: str, cards: List[Dict[str, Any]]) -> tuple[set[str], Dict[str, set[str]], set[str]]:
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    exact, stems = _alias_maps(cards)
    public_terms = _privacy_terms(" ".join(mapping.get("spoken_segments", [])))
    recipient_terms: Dict[str, set[str]] = {}
    protected_terms: set[str] = set()
    alias_terms: set[str] = set()

    for stage in mapping.get("stage_directions", []):
        stage_text = str(stage or "")
        for regex in (_COMMUNICATION_RE, _CHAT_RE):
            for match in regex.finditer(stage_text):
                cid = _resolve_recipient(match.group(1), exact, stems)
                if not cid:
                    continue
                payload = _communication_payload(stage_text, match.end())
                if payload:
                    terms = _privacy_terms(payload)
                    recipient_terms.setdefault(cid, set()).update(terms)
                    protected_terms.update(terms)

        # A character name that occurs only inside private POV text must not become known to bystanders.
        for alias, cid in exact.items():
            if alias and alias in _privacy_norm(stage_text):
                term = _privacy_stem(alias.split()[0])
                if len(term) >= 3:
                    alias_terms.add(term)

    protected_terms.difference_update(public_terms)
    alias_terms.difference_update(public_terms)
    return protected_terms, recipient_terms, alias_terms


def _simple_authorized_corpus(root, character_id: str, cards: List[Dict[str, Any]]) -> str:
    card = next((row for row in cards if storage._card_id(row) == character_id), None)
    pieces: List[str] = []
    if isinstance(card, dict):
        pieces.append(render_character_profile(card))
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    bucket = buckets.get(character_id, {}) if isinstance(buckets.get(character_id), dict) else {}
    journal = bucket.get("knowledge_journal", [])
    if isinstance(journal, list):
        pieces.append(render_knowledge_journal(journal))
    return "\n".join(piece for piece in pieces if piece)


def _speaker_units(scene_output: str, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    exact, _ = _alias_maps(cards)
    result: List[Dict[str, Any]] = []
    for match in _SPEECH_RE.finditer(str(scene_output or "")):
        speaker = _privacy_norm(match.group("speaker"))
        cid = exact.get(speaker) or exact.get(speaker.split()[0] if speaker else "")
        if cid:
            result.append({
                "character_id": cid,
                "text": match.group("text").strip(),
                "position": match.start(),
            })
    return result


def _private_term_violation(
    text: str,
    *,
    protected_terms: set[str],
    alias_terms: set[str],
    allowed_terms: set[str],
) -> List[str]:
    used = _privacy_terms(text)
    leaked = (used & (protected_terms | alias_terms)) - allowed_terms
    # Keep the hard gate conservative: one distinctive long token is enough, otherwise require two.
    strong = sorted(term for term in leaked if len(term) >= 6)
    if strong:
        return strong
    return sorted(leaked) if len(leaked) >= 2 else []


def _validate_simple_private_input_boundary(root, payload: Dict[str, Any]) -> None:
    user_input = str(payload.get("user_input") or "")
    if "(" not in user_input:
        return
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    protected_terms, recipient_terms, alias_terms = _private_input_access(user_input, cards)
    if not protected_terms and not alias_terms:
        return

    mapping = writer_first_runtime._parse_player_input(user_input)
    public_text = " ".join(mapping.get("spoken_segments", []))
    units = _speaker_units(str(payload.get("scene_output") or ""), cards)
    earlier_speech: List[str] = []

    for unit in units:
        cid = str(unit.get("character_id") or "")
        if not cid:
            continue
        if cid == pov_id:
            earlier_speech.append(str(unit.get("text") or ""))
            continue
        authorized = _simple_authorized_corpus(root, cid, cards)
        authorized = "\n".join([
            authorized,
            public_text,
            " ".join(earlier_speech),
            " ".join(recipient_terms.get(cid, set())),
        ])
        allowed_terms = _privacy_terms(authorized)
        leaked = _private_term_violation(
            str(unit.get("text") or ""),
            protected_terms=protected_terms - recipient_terms.get(cid, set()),
            alias_terms=alias_terms,
            allowed_terms=allowed_terms,
        )
        if leaked:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "V5_PRIVATE_INPUT_KNOWLEDGE_LEAK",
                    "character_id": cid,
                    "leaked_terms": leaked,
                    "instruction": (
                        "Перепиши сцену: NPC использовал содержание приватного POV-контекста из ( ), "
                        "которое не было произнесено, показано или адресовано этому персонажу. "
                        "Явная коммуникация внутри ( ) доступна только указанному получателю."
                    ),
                },
            )
        earlier_speech.append(str(unit.get("text") or ""))

    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    rows = extracted.get("knowledge_journal_add")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("character_id") or "")
            if not cid or cid == pov_id:
                continue
            authorized = _simple_authorized_corpus(root, cid, cards)
            authorized = "\n".join([
                authorized,
                public_text,
                " ".join(recipient_terms.get(cid, set())),
            ])
            leaked = _private_term_violation(
                str(row.get("text") or row.get("fact") or ""),
                protected_terms=protected_terms - recipient_terms.get(cid, set()),
                alias_terms=alias_terms,
                allowed_terms=_privacy_terms(authorized),
            )
            if leaked:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "V5_PRIVATE_INPUT_JOURNAL_LEAK",
                        "character_id": cid,
                        "leaked_terms": leaked,
                        "instruction": (
                            "Не сохраняй персонажу знание из приватного POV-контекста. "
                            "Сначала должен существовать реальный доступ: произнесённая речь, наблюдение, показ или адресованное сообщение."
                        ),
                    },
                )


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not _simple_session(root):
        return _ORIGINAL_COMMIT(session_id, payload)

    _validate_simple_private_input_boundary(root, payload)

    prepared = deepcopy(payload)
    extracted = prepared.get("extracted")
    if isinstance(extracted, dict):
        _prepare_journal_entries(root, extracted)
        _normalize_upserts(root, extracted)
        extracted["turn_knowledge"] = []
        extracted["knowledge_usage"] = []
        extracted["knowledge_trace_complete"] = True
        prepared["extracted"] = extracted
    return _ORIGINAL_COMMIT(session_id, prepared)


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not _simple_session(root):
        return dict(_ORIGINAL_PARTICIPATION_BUNDLE(session_id, character_id))

    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    card = next((row for row in cards if storage._card_id(row) == character_id), None)
    if card is None:
        raise KeyError(character_id)

    state = storage._read_json(root / "state.json", {})
    network = npc_relationship_runtime.build_network(
        cards,
        state,
        resolve_character_id=session_runtime._resolve_character_id,
    )
    runtime = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    current_state = runtime.get(character_id, {}) if isinstance(runtime.get(character_id), dict) else {}
    relationship = storage._relationship_hint(state, character_id)

    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = storage._memory_bucket(memory, character_id)
    journal = deepcopy(
        bucket.get("knowledge_journal", [])
        if isinstance(bucket.get("knowledge_journal"), list)
        else []
    )
    legacy_knowledge = deepcopy(
        bucket.get("knowledge", [])
        if isinstance(bucket.get("knowledge"), list)
        else []
    )

    return {
        "character_id": character_id,
        "profile": render_character_profile(card),
        "current_state": deepcopy(current_state),
        "relationship_to_pov": deepcopy(relationship),
        "relationship_footer_snapshot": relationship_runtime.relationship_footer_snapshot_for_character(
            state,
            cards=cards,
            character_id=character_id,
            resolve_character_id=session_runtime._resolve_character_id,
        ),
        "npc_relationships_director_only": npc_relationship_runtime.outgoing_relations_for_character(
            network,
            character_id,
        ),
        "knowledge_journal": journal,
        "legacy_knowledge": legacy_knowledge,
        "knowledge_complete": True,
        "knowledge_scope": {
            "own_card_is_self_known_except_explicit_hidden_branches": True,
            "forbidden_self_branches": [
                "unknown_to_self",
                "hidden_from_self",
                "not_known_to_self",
                "known_to_self=false",
                "author_only",
            ],
            "rule": (
                "Персонаж знает self-known части собственной биографии/profile и собственный knowledge journal. "
                "Каждая запись знания частична ровно до сообщённых деталей: неизвестные время, место, участник, причина или план "
                "не достраиваются вероятными значениями. Нужную неизвестную деталь можно уточнить вопросом; предположение остаётся "
                "предположением до подтверждения. Явно скрытые от него ветки своего profile, чужие профили, чужая память, chronology, "
                "hidden lore и приватные мысли POV знанием не становятся."
            ),
        },
        "instruction": (
            "Этот bundle полностью готов для участия offscreen-персонажа: own profile + own complete knowledge "
            "+ relationship + current state. relationship_footer_snapshot сохраняет точные старые NPC→POV labels/values для footer, "
            "если персонаж войдёт физически в текущем ходу; не выбрасывай неизменившиеся оси. npc_relationships_director_only влияет на режиссуру поведения, но не является "
            "личным factual knowledge и не сообщает персонажу неизвестные факты о другом NPC. Используй только явно известные "
            "детали; не дополняй частичный факт скрытыми или вероятными подробностями. Отдельный knowledge-read не нужен."
        ),
    }


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT, _ORIGINAL_PARTICIPATION_BUNDLE
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    _ORIGINAL_PARTICIPATION_BUNDLE = character_chunk_read._participation_bundle
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
    character_chunk_read._participation_bundle = _participation_bundle
