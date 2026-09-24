from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import character_chunk_read, session_runtime, storage, writer_first_runtime
from .profile_templates import (
    normalize_character_profile,
    render_character_profile,
    render_hidden_lore,
    render_knowledge_journal,
    render_novel_profile,
)
from .transactional_storage import session_transaction


_PROFILE_RUNTIME_VERSION = 1
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


def _journal_map(memory: Dict[str, Any], ids: List[str]) -> Dict[str, str]:
    buckets = memory.get("characters", {}) if isinstance(memory.get("characters"), dict) else {}
    result: Dict[str, str] = {}
    for cid in ids:
        bucket = buckets.get(cid, {}) if isinstance(buckets.get(cid), dict) else {}
        journal = bucket.get("knowledge_journal", [])
        if not isinstance(journal, list):
            journal = []
        result[cid] = render_knowledge_journal(journal)
    return result


def _speaker_context(ids: List[str], pov_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for cid in ids:
        result[cid] = {
            "character_id": cid,
            "profile_path": f"character_profiles[{cid}]",
            "knowledge_journal_path": f"knowledge_journals[{cid}]",
            "current_perception": "only what this character can see/hear/receive in the current scene",
            "relationship_path": f"relationship_lens.relations_in_current_scene[owner_character_id={cid}]",
            "rule": (
                "Реплики и решения этого персонажа строятся отдельно: кто он по своему профилю, "
                "что лично знает из своего журнала, что видит/слышит сейчас и как относится к POV. "
                "Чужие профили, чужие журналы, chronology и director_only не являются его знаниями."
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
        memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        state = storage._read_json(root / "state.json", {})
        pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
        pov_id = str(pov.get("character_id") or "")

        ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
        if pov_id and pov_id not in ids:
            ids.insert(0, pov_id)
        if not ids:
            ids = storage._present_character_ids(state)

        profiles = _profile_map(cards, ids)
        journals = _journal_map(memory, ids)

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
                    row["memory_path"] = f"knowledge_journals[{cid}]"
            context["scene_presence"] = scene_presence

        context["novel_profile"] = render_novel_profile(source.get("novel", {}))
        context["character_profiles"] = profiles
        context["knowledge_journals"] = journals
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
                    frame["memory_path"] = f"knowledge_journals[{cid}]"
                    frame["instruction"] = (
                        "Поведение: character_drivers + отношения + intents. Фактическое знание: только собственный "
                        "profile, knowledge_journal и текущее восприятие."
                    )
            context["living_world"] = living

        guards = context.get("scene_logic_guardrails")
        if isinstance(guards, dict):
            guards = deepcopy(guards)
            guards["knowledge_causality"] = {
                "mandatory": True,
                "applies_to": "speech, POV narration and POV inner view",
                "rule": (
                    "NPC: own profile + own knowledge_journal + current perception. POV: own profile + own "
                    "knowledge_journal + current perception. Chronology, hidden lore and other profiles are director-only."
                ),
            }
            guards["knowledge_review"] = {
                "mandatory": True,
                "rule": (
                    "Before commit check every speaking character separately against speaker_context. "
                    "No fact/source ledger is required in simple-profile mode."
                ),
            }
            context["scene_logic_guardrails"] = guards

        context["simple_knowledge_rules"] = {
            "version": _PROFILE_RUNTIME_VERSION,
            "npc": (
                "Для каждого NPC отдельно: собственный profile + собственный knowledge_journal + доступное ему "
                "текущее восприятие + отношения. Ничего больше не считать его знанием."
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
            "knowledge_transport": (
                "Для каждого участника сцены knowledge_journal передаётся полностью, без лимита по числу записей."
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


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not _simple_session(root):
        return _ORIGINAL_COMMIT(session_id, payload)

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
    runtime = state.get("characters", {}) if isinstance(state.get("characters"), dict) else {}
    current_state = runtime.get(character_id, {}) if isinstance(runtime.get(character_id), dict) else {}
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    bucket = storage._memory_bucket(memory, character_id)
    journal = bucket.get("knowledge_journal", []) if isinstance(bucket.get("knowledge_journal"), list) else []

    relationship = storage._relationship_hint(state, character_id)
    return {
        "character_id": character_id,
        "profile": render_character_profile(card),
        "knowledge_journal": render_knowledge_journal(journal),
        "current_state": deepcopy(current_state),
        "relationship_to_pov": deepcopy(relationship),
        "instruction": (
            "Этот bundle принадлежит только этому персонажу. knowledge_journal передан полностью. Для реплик используй "
            "собственный profile, полный knowledge_journal и текущее восприятие. Чужие данные и chronology не являются его знаниями."
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
