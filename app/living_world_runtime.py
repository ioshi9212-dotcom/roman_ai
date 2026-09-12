from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import runtime_fixes as base
from . import runtime_fixes_compat as compat
from . import session_runtime, storage, writer_first_runtime
from .transactional_storage import session_transaction


_ORIGINAL_PREPARE = None
_ORIGINAL_COMMIT = None
_VERSION = 1

# New labels come from one small shared vocabulary. Existing labels in old sessions remain valid.
RELATIONSHIP_DIMENSIONS: Dict[str, str] = {
    "доверие": "насколько NPC верит словам, надёжности и намерениям POV",
    "близость": "насколько естественны личность, фамильярность и малая дистанция",
    "привязанность": "насколько POV стала личным приоритетом и частью жизни NPC",
    "симпатия": "тепло, доброжелательность и удовольствие от контакта",
    "влечение": "физическое или романтическое притяжение и инициатива",
    "уважение": "вес мнения, способностей и решений POV для NPC",
    "подозрение": "склонность сомневаться, проверять и искать скрытый смысл",
    "настороженность": "общая осторожность и готовность ждать подвоха",
    "раздражение": "текущая терпимость и резкость реакции",
    "обида": "удерживаемое переживание нанесённого вреда или унижения",
    "ревность": "чувствительность к чужому вниманию и соперникам",
    "страх": "ожидание угрозы от POV и осторожность рядом с ней",
    "соперничество": "потребность сравниваться, спорить, побеждать или не уступать",
}
_ALLOWED_NORMS = {base._relationship_norm(label) for label in RELATIONSHIP_DIMENSIONS}
_HOOK_WORDS = (
    "past", "history", "background", "family", "brother", "sister", "mother", "father",
    "fear", "weak", "goal", "secret", "trauma", "прошл", "истор", "семь", "брат", "сестр",
    "мать", "отец", "страх", "слаб", "цель", "тайн", "травм",
)


def _name(card: Dict[str, Any], fallback: str) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(card.get("name") or card.get("full_name") or identity.get("name") or fallback)


def _card_map(cards: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        storage._card_id(card): card
        for card in cards
        if isinstance(card, dict) and storage._card_id(card)
    }


def _relation_for(state: Dict[str, Any], owner_id: str) -> Dict[str, Any] | None:
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    doc = docs.get(owner_id) if isinstance(docs.get(owner_id), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    for relation in doc.get("relations", []) if isinstance(doc.get("relations"), list) else []:
        if isinstance(relation, dict) and str(relation.get("target_character_id") or "") == pov_id:
            return relation
    return None


def _existing_metrics(state: Dict[str, Any], owner_id: str) -> Dict[str, float]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    row = relationships.get(owner_id) if isinstance(relationships.get(owner_id), dict) else {}
    return {
        base._relationship_norm(label): float(value)
        for label, value in row.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _incoming_label_set(dimensions: Any) -> set[str]:
    if not isinstance(dimensions, list):
        return set()
    return {
        base._relationship_norm(str(item.get("label") or item.get("key") or ""))
        for item in dimensions
        if isinstance(item, dict) and str(item.get("label") or item.get("key") or "").strip()
    }


def _validate_labels(owner_id: str, dimensions: Any, state: Dict[str, Any]) -> None:
    if not isinstance(dimensions, list):
        return
    saved = _existing_metrics(state, owner_id)
    # relationship_updates are patches, so omitted saved labels are expected and remain persistent.
    # Validate only labels that are actually being introduced by this update.
    legacy = set(saved)
    for item in dimensions:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("key") or "").strip()
        if not label:
            continue
        normalized = base._relationship_norm(label)
        if normalized in legacy or normalized in _ALLOWED_NORMS:
            continue
        raise HTTPException(
            status_code=409,
            detail={
                "code": "RELATIONSHIP_DIMENSION_UNKNOWN",
                "message": (
                    f"{owner_id}: do not invent relationship metric {label!r}. "
                    "Use the fixed relationship vocabulary from living_world; old saved labels stay valid."
                ),
            },
        )


def _validate_relationship_vocabulary(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    # The visible footer is display-only. Invalid display labels must not block a gameplay commit.
    # Canonical numeric writes are validated only from extracted.relationship_updates.
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    updates = extracted.get("relationship_updates") if isinstance(extracted.get("relationship_updates"), list) else []
    for raw in updates:
        if not isinstance(raw, dict):
            continue
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if owner_id:
            _validate_labels(str(owner_id), raw.get("dimensions"), state)


def _packet_relevant_ids(packet: Dict[str, Any]) -> set[str]:
    try:
        context = json.loads("".join(str(chunk) for chunk in packet.get("chunks", [])))
    except (TypeError, json.JSONDecodeError):
        return set()
    return {str(value) for value in context.get("relevant_character_ids", []) if value}


def _split_relationship_metadata(
    session_id: str, payload: Dict[str, Any]
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    result = deepcopy(payload)
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    rows = extracted.get("relationship_updates")
    if not isinstance(rows, list):
        return result, []

    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    packet = storage._read_json(root / "turn_packet.json", {})
    allowed_ids = {str(value) for value in storage._present_character_ids(state)}
    allowed_ids.update(_packet_relevant_ids(packet))
    allowed_ids.update(
        storage._card_id(row)
        for row in extracted.get("character_upserts", [])
        if isinstance(row, dict) and storage._card_id(row)
    )

    metadata: List[Dict[str, Any]] = []
    dimension_updates: List[Dict[str, Any]] = []
    meta_keys = (
        "opinion",
        "current_dynamic",
        "beliefs_about_target",
        "unresolved_between_them",
        "relationship_type",
        "relationship_context",
        "reason",
        "change_scale",
        "elapsed_game_days",
    )
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        owner_id = base._resolve_character_id(cards, raw.get("character_id")) or str(raw.get("character_id") or "")
        meta = {key: deepcopy(raw[key]) for key in meta_keys if key in raw}
        if meta:
            if isinstance(dims, list) and dims:
                meta["_numeric_changes"] = [
                    {
                        "label": str(item.get("label") or item.get("key") or ""),
                        "delta": item.get("delta"),
                    }
                    for item in dims
                    if isinstance(item, dict) and item.get("delta") is not None
                ]
            if not owner_id or owner_id not in allowed_ids:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "RELATIONSHIP_OPINION_UNSEEN_NPC",
                        "message": "Relationship opinion may change only for an NPC participating in this turn.",
                    },
                )
            meta["character_id"] = owner_id
            metadata.append(meta)
        dims = raw.get("dimensions")
        if isinstance(dims, list) and dims:
            dimension_updates.append({
                "character_id": owner_id,
                "dimensions": deepcopy(dims),
                "reason": raw.get("reason"),
                "change_scale": raw.get("change_scale"),
                "elapsed_game_days": raw.get("elapsed_game_days"),
            })

    extracted = deepcopy(extracted)
    extracted["relationship_updates"] = dimension_updates
    result["extracted"] = extracted
    return result, metadata


def _flatten(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    if depth > 3:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _flatten(child, child_path, depth + 1)
    elif isinstance(value, list):
        for child in value[:8]:
            yield from _flatten(child, path, depth + 1)
    elif isinstance(value, (str, int, float, bool)):
        text = " ".join(str(value).split())
        if text:
            yield path, text[:240]


def _driver_snippets(card: Dict[str, Any]) -> List[Dict[str, str]]:
    wanted = (
        "person", "character", "temper", "goal", "motivat", "fear", "weak", "habit",
        "характ", "цель", "мотив", "страх", "слаб", "привыч",
    )
    result: List[Dict[str, str]] = []
    for path, text in _flatten(card):
        if any(token in path.casefold() for token in wanted):
            result.append({"path": path, "value": text})
            if len(result) >= 6:
                break
    return result


def _actor_frames(
    context: Dict[str, Any], state: Dict[str, Any], cards: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    by_id = _card_map(cards)
    ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    intents = context.get("npc_active_intents") if isinstance(context.get("npc_active_intents"), dict) else {}
    result: List[Dict[str, Any]] = []
    for cid in ids:
        if not cid or cid == pov_id or cid not in by_id:
            continue
        relation = _relation_for(state, cid) or {}
        flat = state.get("relationships", {}).get(cid, {}) if isinstance(state.get("relationships"), dict) else {}
        dimensions = deepcopy(relation.get("dimensions", []))
        if not dimensions and isinstance(flat, dict):
            dimensions = [
                {"label": label, "value": value}
                for label, value in flat.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
        result.append({
            "character_id": cid,
            "name": _name(by_id[cid], cid),
            "character_drivers": _driver_snippets(by_id[cid]),
            "relationship": {
                "dimensions": dimensions,
                "current_opinion": relation.get("current_dynamic") or "",
                "beliefs_about_pov": deepcopy(relation.get("beliefs_about_target", [])),
                "unresolved_between_them": deepcopy(relation.get("unresolved_between_them", [])),
            },
            "active_intents": deepcopy(intents.get(cid, [])) if isinstance(intents.get(cid), list) else [],
            "memory_path": f"character_memory[{cid}]",
            "instruction": (
                "Play this NPC from this exact combination. Ask what this person wants now, "
                "what they think POV is like, what they know, and what they would actually do."
            ),
        })
    return result


def _relationship_strength(metrics: Any) -> float:
    if not isinstance(metrics, dict):
        return 0.0
    values = [
        abs(float(value))
        for value in metrics.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not values:
        return 0.0
    maximum = max(values)
    return min(1.0, maximum / 10.0 if maximum <= 10 else maximum / 100.0)


def _enrich_cast_pressure(context: Dict[str, Any], state: Dict[str, Any]) -> None:
    guards = context.get("narrative_guardrails") if isinstance(context.get("narrative_guardrails"), dict) else {}
    rows = guards.get("cast_pressure") if isinstance(guards.get("cast_pressure"), list) else []
    existing = {str(row.get("character_id")) for row in rows if isinstance(row, dict)}
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    cast = context.get("cast_index") if isinstance(context.get("cast_index"), list) else []
    for member in cast:
        if not isinstance(member, dict) or member.get("present") or member.get("is_pov"):
            continue
        cid = str(member.get("character_id") or "")
        strength = _relationship_strength(relationships.get(cid))
        if not cid or cid in existing or strength < 0.6:
            continue
        rows.append({
            "character_id": cid,
            "name": member.get("name") or member.get("full_name"),
            "relationship_salience": round(strength, 2),
            "guidance": (
                "This relationship is strong enough to affect initiative/frequency. "
                "Let the NPC contact, appear, avoid or interfere only when character and circumstances give a reason."
            ),
        })
        existing.add(cid)
    guards["cast_pressure"] = rows[:8]
    context["narrative_guardrails"] = guards


def _foundation(source: Dict[str, Any]) -> Dict[str, Any]:
    value = source.get("foundation")
    return value if isinstance(value, dict) else {}


def _recent_text(context: Dict[str, Any]) -> str:
    parts = []
    for key in ("recent_turns", "continuity_turns", "chronology_recent"):
        if context.get(key):
            parts.append(json.dumps(context[key], ensure_ascii=False))
    return " ".join(parts).casefold()


def _foundation_pressure(
    source: Dict[str, Any], state: Dict[str, Any], context: Dict[str, Any],
    cards: List[Dict[str, Any]], current_turn: int,
) -> Dict[str, Any]:
    foundation = _foundation(source)
    recent = _recent_text(context)
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    hook_state = world.get("foundation_hook_state") if isinstance(world.get("foundation_hook_state"), dict) else {}
    facts = {
        str(row.get("fact_id")): row
        for row in foundation.get("facts", [])
        if isinstance(row, dict) and row.get("fact_id")
    }
    candidates = []
    for hook in foundation.get("hooks", []) if isinstance(foundation.get("hooks"), list) else []:
        if not isinstance(hook, dict) or not hook.get("hook_id"):
            continue
        hid = str(hook["hook_id"])
        live = hook_state.get(hid) if isinstance(hook_state.get(hid), dict) else {}
        status = str(live.get("status") or hook.get("status") or "latent").casefold()
        if status in {"resolved", "closed", "abandoned"}:
            continue
        last_touch = int(live.get("last_touched_turn", 0) or 0)
        if last_touch and current_turn - last_touch < 12:
            continue
        linked = [facts.get(str(fid)) for fid in hook.get("fact_ids", []) if facts.get(str(fid))]
        snippets = [str(row.get("text") or "") for row in linked if isinstance(row, dict)]
        short = [text for text in snippets if text and len(text) < 120]
        if short and all(text.casefold() in recent for text in short):
            continue
        candidates.append({
            "hook_id": hid,
            "fact_ids": [str(fid) for fid in hook.get("fact_ids", [])],
            "facts": snippets[:3],
            "condition": hook.get("condition") or hook.get("when"),
            "pillar_ids": deepcopy(hook.get("pillar_ids", [])),
            "last_touched_turn": last_touch,
        })
        if len(candidates) >= 6:
            break

    legacy_cues = []
    if not foundation:
        for card in cards:
            cid = storage._card_id(card)
            for path, text in _flatten(card):
                if any(token in path.casefold() for token in _HOOK_WORDS):
                    legacy_cues.append({"character_id": cid, "path": path, "fact": text})
                    if len(legacy_cues) >= 6:
                        break
            if len(legacy_cues) >= 6:
                break

    return {
        "mandatory_consideration": True,
        "eligible_hooks": candidates,
        "legacy_card_cues": legacy_cues,
        "instruction": (
            "Setup facts are not decoration. Use eligible facts gradually when a natural memory, reaction, clue, "
            "conversation, consequence or plot opening makes them relevant. Do not dump several hooks at once."
        ),
    }


def _pillar_row(pid: str, label: str, last: int, current_turn: int, **extra: Any) -> Dict[str, Any]:
    age = max(0, current_turn - last)
    row = {
        "pillar_id": pid,
        "label": label,
        "turns_since_influence": age,
        "attention_due": current_turn >= 18 and age >= 18,
        "strongly_due": current_turn >= 36 and age >= 36,
    }
    row.update(extra)
    return row


def _story_pillars(source: Dict[str, Any], state: Dict[str, Any], current_turn: int) -> Dict[str, Any]:
    foundation = _foundation(source)
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    persisted = world.get("story_pillars") if isinstance(world.get("story_pillars"), dict) else {}
    rows = []
    for raw in foundation.get("story_pillars", []) if isinstance(foundation.get("story_pillars"), list) else []:
        if not isinstance(raw, dict) or not raw.get("pillar_id"):
            continue
        pid = str(raw["pillar_id"])
        live = persisted.get(pid) if isinstance(persisted.get(pid), dict) else {}
        rows.append(_pillar_row(
            pid,
            str(raw.get("label") or raw.get("name") or pid),
            int(live.get("last_touched_turn", 0) or 0),
            current_turn,
            source_fact_ids=deepcopy(raw.get("source_fact_ids", [])),
        ))

    if not rows:
        novel = source.get("novel") if isinstance(source.get("novel"), dict) else {}
        raw_genres = novel.get("genres") or novel.get("genre") or source.get("genre")
        if isinstance(raw_genres, str):
            raw_genres = [raw_genres]
        if isinstance(raw_genres, list):
            for index, genre in enumerate(raw_genres[:8]):
                pid = f"legacy_genre_{index + 1}"
                live = persisted.get(pid) if isinstance(persisted.get(pid), dict) else {}
                rows.append(_pillar_row(
                    pid,
                    str(genre),
                    int(live.get("last_touched_turn", 0) or 0),
                    current_turn,
                    legacy_derived=True,
                ))

    return {
        "mandatory_consideration": True,
        "pillars": rows,
        "instruction": (
            "A story pillar must sometimes change what actually happens, not only color descriptions. "
            "If one has been absent for a long time, use an existing causal hook/thread/world condition. No quota and no random genre event."
        ),
    }


def _social_world(state: Dict[str, Any]) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    social = world.get("social") if isinstance(world.get("social"), dict) else {}
    signals = social.get("signals") if isinstance(social.get("signals"), dict) else {}
    return {
        "mandatory": True,
        "recent_social_signals": deepcopy(list(signals.values())[-12:]),
        "instruction": (
            "Background people are people, not wallpaper. Salient appearance, behavior, status, conflict or repeated presence "
            "may be noticed, misread, discussed or remembered when plausible. A recurring/important extra gets character_upserts. "
            "Durable social fallout belongs in chronology.social_effect. Rumors need real witnesses/channels."
        ),
    }


def _rewrite_packet(session_id: str, base_result: Dict[str, Any]) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    with session_transaction(root):
        packet = storage._read_json(root / "turn_packet.json", {})
        if not isinstance(packet, dict) or not packet.get("chunks"):
            return base_result
        if packet.get("living_world_version") == _VERSION:
            return base_result
        raw = "".join(str(chunk) for chunk in packet.get("chunks", []))
        try:
            context = json.loads(raw)
        except json.JSONDecodeError:
            return base_result

        source = storage._read_json(root / "source.json", {})
        state = storage._read_json(root / "state.json", {})
        cards = storage._load_cards(root, source)
        meta = storage._read_json(root / "meta.json", {})
        current_turn = int(meta.get("turn_number", 0) or 0)
        _enrich_cast_pressure(context, state)

        context["living_world"] = {
            "mandatory": True,
            "relationship_model": {
                "fixed_new_dimensions": deepcopy(RELATIONSHIP_DIMENSIONS),
                "legacy_rule": "Existing saved legacy labels stay valid and are never renamed to fit the new vocabulary.",
                "numbers_are_behavioral": "Values affect tone, initiative, distance, interpretation and choices when relevant; they are not decorative counters.",
                "opinion_rule": "current_opinion/beliefs may be wrong or biased. Keep the latest belief until something actually changes it.",
            },
            "npc_actor_frames": _actor_frames(context, state, cards),
            "foundation_pressure": _foundation_pressure(source, state, context, cards, current_turn),
            "story_pillar_pressure": _story_pillars(source, state, current_turn),
            "social_reactivity": _social_world(state),
            "instruction": (
                "Before writing an NPC, use the actor frame: character + wants + knowledge + relationship + opinion + unresolved business. "
                "NPCs and the social world can create initiative and consequences instead of waiting for POV."
            ),
        }
        persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
        persistence["relationship_opinion"] = (
            "When opinion/beliefs/unresolved state changes, use relationship_updates with character_id plus opinion/current_dynamic "
            "and/or full beliefs_about_target/unresolved_between_them. Numeric relationship changes also use relationship_updates; "
            "the visible footer is display-only."
        )
        persistence["social_effect"] = "Durable social reaction/rumor/reputation: put social_effect inside the relevant chronology event."
        persistence["foundation_fact_ids"] = "When a foundation fact is actually used, attach foundation_fact_ids to chronology or anchor_facts to the story thread."
        persistence["story_pillar_ids"] = "When a story pillar actually influences the turn, attach story_pillar_ids to chronology or pillar_ids to story_thread_updates."
        context["persistence_contract"] = persistence

        text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        size = writer_first_runtime.WRITER_PACKET_CHARS
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or ["{}"]
        packet["chunks"] = chunks
        packet["chunk_count"] = len(chunks)
        packet["read_chunks"] = [0]
        packet["living_world_version"] = _VERSION
        storage._write_json(root / "turn_packet.json", packet)

        result = dict(base_result)
        result.update({
            "chunk_count": len(chunks),
            "total_chars": len(text),
            "first_chunk_included": True,
            "chunk_index": 0,
            "content": chunks[0],
            "all_chunks_read": len(chunks) == 1,
            "next_chunk_index": None if len(chunks) == 1 else 1,
            "living_world": True,
        })
        return result


def _extract_fact_ids(extracted: Dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in extracted.get("chronology", []) if isinstance(extracted.get("chronology"), list) else []:
        if not isinstance(row, dict):
            continue
        values = row.get("foundation_fact_ids")
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list):
            result.update(str(value) for value in values if value)
    for row in extracted.get("story_thread_updates", []) if isinstance(extracted.get("story_thread_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        values = row.get("anchor_facts")
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list):
            result.update(str(value) for value in values if value)
    return result


def _extract_pillar_ids(extracted: Dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in extracted.get("chronology", []) if isinstance(extracted.get("chronology"), list) else []:
        if not isinstance(row, dict):
            continue
        values = row.get("story_pillar_ids")
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list):
            result.update(str(value) for value in values if value)
    for row in extracted.get("story_thread_updates", []) if isinstance(extracted.get("story_thread_updates"), list) else []:
        if not isinstance(row, dict):
            continue
        values = row.get("pillar_ids")
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list):
            result.update(str(value) for value in values if value)
    return result


def _apply_world_effects_to_state(
    root,
    state: Dict[str, Any],
    extracted: Dict[str, Any],
    turn_number: int,
) -> tuple[Dict[str, Any], bool]:
    result = deepcopy(state)
    world = deepcopy(result.get("world") if isinstance(result.get("world"), dict) else {})
    changed = False

    social = deepcopy(world.get("social") if isinstance(world.get("social"), dict) else {})
    signals = deepcopy(social.get("signals") if isinstance(social.get("signals"), dict) else {})
    for index, row in enumerate(extracted.get("chronology", []) if isinstance(extracted.get("chronology"), list) else []):
        if not isinstance(row, dict) or not isinstance(row.get("social_effect"), dict):
            continue
        effect = deepcopy(row["social_effect"])
        effect["turn"] = turn_number
        signals[f"t{turn_number}_{index + 1}"] = effect
        changed = True
    if len(signals) > 100:
        signals = dict(list(signals.items())[-100:])
    if signals:
        social["signals"] = signals
        world["social"] = social

    source = storage._read_json(root / "source.json", {})
    foundation = _foundation(source)
    fact_ids = _extract_fact_ids(extracted)
    pillar_ids = _extract_pillar_ids(extracted)

    if fact_ids:
        hook_state = deepcopy(world.get("foundation_hook_state") if isinstance(world.get("foundation_hook_state"), dict) else {})
        for hook in foundation.get("hooks", []) if isinstance(foundation.get("hooks"), list) else []:
            if not isinstance(hook, dict) or not hook.get("hook_id"):
                continue
            if not fact_ids.intersection(str(fid) for fid in hook.get("fact_ids", [])):
                continue
            hid = str(hook["hook_id"])
            live = deepcopy(hook_state.get(hid) if isinstance(hook_state.get(hid), dict) else {})
            live["status"] = "touched"
            live["last_touched_turn"] = turn_number
            live["touch_count"] = int(live.get("touch_count", 0) or 0) + 1
            hook_state[hid] = live
            pillar_ids.update(str(pid) for pid in hook.get("pillar_ids", []) if pid)
        world["foundation_hook_state"] = hook_state
        for raw in foundation.get("story_pillars", []) if isinstance(foundation.get("story_pillars"), list) else []:
            if not isinstance(raw, dict) or not raw.get("pillar_id"):
                continue
            sources = {str(fid) for fid in raw.get("source_fact_ids", []) if fid}
            if sources.intersection(fact_ids):
                pillar_ids.add(str(raw["pillar_id"]))
        changed = True

    if pillar_ids:
        known = {
            str(raw.get("pillar_id")): raw
            for raw in foundation.get("story_pillars", [])
            if isinstance(raw, dict) and raw.get("pillar_id")
        }
        pillars = deepcopy(world.get("story_pillars") if isinstance(world.get("story_pillars"), dict) else {})
        for pid in pillar_ids:
            live = deepcopy(pillars.get(pid) if isinstance(pillars.get(pid), dict) else {})
            if pid in known:
                live.setdefault("label", known[pid].get("label") or known[pid].get("name") or pid)
                live.setdefault("source_fact_ids", deepcopy(known[pid].get("source_fact_ids", [])))
            live["last_touched_turn"] = turn_number
            live.setdefault("status", "active")
            pillars[pid] = live
        world["story_pillars"] = pillars
        changed = True

    if changed:
        result["world"] = world
    return result, changed


def _with_atomic_state_effects(
    session_id: str,
    payload: Dict[str, Any],
    metadata: List[Dict[str, Any]],
) -> Dict[str, Any]:
    result = deepcopy(payload)
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    meta = storage._read_json(root / "meta.json", {})
    turn_number = int(meta.get("turn_number", 0) or 0) + 1
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    extracted = deepcopy(extracted)
    patch = extracted.get("state_patch") if isinstance(extracted.get("state_patch"), dict) else {}
    patch = deepcopy(patch)
    post_state = storage._deep_merge(state, patch)

    post_state, world_changed = _apply_world_effects_to_state(root, post_state, extracted, turn_number)
    if world_changed:
        patch["world"] = deepcopy(post_state.get("world", {}))
    extracted["state_patch"] = patch
    result["extracted"] = extracted
    if metadata:
        result["_relationship_metadata"] = deepcopy(metadata)
    return result

def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_relationship_vocabulary(session_id, payload)
    prepared, metadata = _split_relationship_metadata(session_id, payload)
    prepared = _with_atomic_state_effects(session_id, prepared, metadata)
    return _ORIGINAL_COMMIT(session_id, prepared)


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
