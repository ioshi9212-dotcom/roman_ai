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

# New relationships use this small shared vocabulary. Legacy labels already stored in old
# sessions are preserved and may continue changing; they are simply not invented anew.
RELATIONSHIP_DIMENSIONS: Dict[str, str] = {
    "доверие": "насколько NPC верит словам, надёжности и намерениям POV",
    "близость": "насколько естественны личность, фамильярность и малая дистанция",
    "привязанность": "насколько POV стала личным приоритетом и частью жизни NPC",
    "симпатия": "тепло, доброжелательность и удовольствие от контакта",
    "влечение": "физическое или романтическое притяжение и инициатива",
    "уважение": "вес мнения, способностей и решений POV для NPC",
    "подозрение": "склонность сомневаться, проверять и искать скрытый смысл",
    "раздражение": "текущая короткая терпимость и резкость реакции",
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
    return {storage._card_id(card): card for card in cards if isinstance(card, dict) and storage._card_id(card)}


def _relation_for(state: Dict[str, Any], owner_id: str) -> Dict[str, Any] | None:
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    doc = docs.get(owner_id) if isinstance(docs.get(owner_id), dict) else {}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    for relation in doc.get("relations", []) if isinstance(doc.get("relations"), list) else []:
        if isinstance(relation, dict) and str(relation.get("target_character_id") or "") == pov_id:
            return relation
    return None


def _existing_labels(state: Dict[str, Any], owner_id: str) -> set[str]:
    relationships = state.get("relationships") if isinstance(state.get("relationships"), dict) else {}
    row = relationships.get(owner_id) if isinstance(relationships.get(owner_id), dict) else {}
    return {base._relationship_norm(label) for label, value in row.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}


def _validate_labels(owner_id: str, dimensions: Any, state: Dict[str, Any]) -> None:
    if not isinstance(dimensions, list):
        return
    legacy = _existing_labels(state, owner_id)
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
                "message": f"{owner_id}: do not invent relationship metric {label!r}. Use the fixed relationship vocabulary already supplied in the turn packet; legacy saved labels may be preserved.",
            },
        )


def _validate_relationship_vocabulary(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    state = storage._read_json(root / "state.json", {})
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    footer = compat._parse_footer_compat(
        str(payload.get("scene_output") or ""), cards=cards, resolve_character_id=base._resolve_character_id
    )
    for owner_id, dimensions in footer.items():
        _validate_labels(str(owner_id), dimensions, state)
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    for raw in extracted.get("relationship_updates", []) if isinstance(extracted.get("relationship_updates"), list) else []:
        if not isinstance(raw, dict):
            continue
        owner_id = base._resolve_character_id(cards, raw.get("character_id"))
        if owner_id:
            _validate_labels(str(owner_id), raw.get("dimensions"), state)


def _split_relationship_metadata(session_id: str, payload: Dict[str, Any]) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
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
    allowed_ids = set(str(value) for value in storage._present_character_ids(state))
    allowed_ids.update(str(value) for value in packet.get("relevant_character_ids", []) if value)
    allowed_ids.update(
        storage._card_id(row) for row in extracted.get("character_upserts", [])
        if isinstance(row, dict) and storage._card_id(row)
    )

    metadata: List[Dict[str, Any]] = []
    dimension_updates: List[Dict[str, Any]] = []
    meta_keys = ("opinion", "current_dynamic", "beliefs_about_target", "unresolved_between_them", "relationship_type", "relationship_context")
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        owner_id = base._resolve_character_id(cards, raw.get("character_id")) or str(raw.get("character_id") or "")
        meta = {key: deepcopy(raw[key]) for key in meta_keys if key in raw}
        if meta:
            if not owner_id or owner_id not in allowed_ids:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "RELATIONSHIP_OPINION_UNSEEN_NPC", "message": "Relationship opinion may change only for an NPC participating in this turn."},
                )
            meta["character_id"] = owner_id
            metadata.append(meta)
        dims = raw.get("dimensions")
        if isinstance(dims, list) and dims:
            dimension_updates.append({"character_id": owner_id, "dimensions": deepcopy(dims)})

    extracted = deepcopy(extracted)
    extracted["relationship_updates"] = dimension_updates
    result["extracted"] = extracted
    return result, metadata


def _apply_relationship_metadata(root, rows: List[Dict[str, Any]], turn_number: int) -> None:
    if not rows:
        return
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    docs = state.get("relationship_documents") if isinstance(state.get("relationship_documents"), dict) else {}
    docs = deepcopy(docs)

    for raw in rows:
        owner_id = str(raw.get("character_id") or "")
        if not owner_id or not pov_id:
            continue
        doc = docs.setdefault(owner_id, {"owner_character_id": owner_id, "relations": []})
        relations = doc.get("relations") if isinstance(doc.get("relations"), list) else []
        relation = next((row for row in relations if isinstance(row, dict) and str(row.get("target_character_id") or "") == pov_id), None)
        if relation is None:
            relation = {
                "target_character_id": pov_id,
                "relationship_type": "установленная связь",
                "relationship_context": "",
                "current_dynamic": "",
                "dimensions": [],
                "beliefs_about_target": [],
                "unresolved_between_them": [],
                "dynamic_constraints": [],
                "change_reasons": [],
                "last_changed_turn": 0,
            }
            relations.append(relation)
            doc["relations"] = relations
        if "opinion" in raw:
            relation["current_dynamic"] = str(raw.get("opinion") or "").strip()
        if "current_dynamic" in raw:
            relation["current_dynamic"] = str(raw.get("current_dynamic") or "").strip()
        for key in ("beliefs_about_target", "unresolved_between_them"):
            if key in raw and isinstance(raw[key], list):
                relation[key] = deepcopy(raw[key])
        for key in ("relationship_type", "relationship_context"):
            if key in raw:
                relation[key] = str(raw.get(key) or "").strip()
        relation["last_changed_turn"] = turn_number

    state["relationship_documents"] = docs
    storage._write_json(root / "state.json", state)


def _flatten_driver_values(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, str]]:
    if depth > 3:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _flatten_driver_values(child, child_path, depth + 1)
    elif isinstance(value, list):
        for child in value[:6]:
            yield from _flatten_driver_values(child, path, depth + 1)
    elif isinstance(value, (str, int, float, bool)):
        text = " ".join(str(value).split())
        if text:
            yield path, text[:220]


def _driver_snippets(card: Dict[str, Any]) -> List[Dict[str, str]]:
    wanted = ("person", "character", "temper", "goal", "motivat", "fear", "weak", "habit", "характ", "цель", "мотив", "страх", "слаб", "привыч")
    result: List[Dict[str, str]] = []
    for path, text in _flatten_driver_values(card):
        if any(token in path.casefold() for token in wanted):
            result.append({"path": path, "value": text})
            if len(result) >= 6:
                break
    return result


def _actor_frames(context: Dict[str, Any], state: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = _card_map(cards)
    ids = [str(value) for value in context.get("relevant_character_ids", []) if value]
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    intents = context.get("npc_active_intents") if isinstance(context.get("npc_active_intents"), dict) else {}
    result = []
    for cid in ids:
        if not cid or cid == pov_id or cid not in by_id:
            continue
        relation = _relation_for(state, cid) or {}
        row = {
            "character_id": cid,
            "name": _name(by_id[cid], cid),
            "character_drivers": _driver_snippets(by_id[cid]),
            "relationship": {
                "dimensions": deepcopy(relation.get("dimensions", [])),
                "current_opinion": relation.get("current_dynamic") or "",
                "beliefs_about_pov": deepcopy(relation.get("beliefs_about_target", [])),
                "unresolved_between_them": deepcopy(relation.get("unresolved_between_them", [])),
            },
            "active_intents": deepcopy(intents.get(cid, [])) if isinstance(intents.get(cid), list) else [],
            "memory_path": f"character_memory[{cid}]",
            "instruction": "Play this NPC from this exact combination. Ask: what does this person want now, what do they think POV is like, what do they know, and what would they actually do?",
        }
        result.append(row)
    return result


def _relationship_strength(metrics: Any) -> float:
    if not isinstance(metrics, dict):
        return 0.0
    values = [abs(float(value)) for value in metrics.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if not values:
        return 0.0
    maximum = max(values)
    return maximum / 10.0 if maximum <= 10 else maximum / 100.0


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
            "guidance": "This relationship is strong enough to affect initiative/frequency. Let the NPC contact, appear, avoid, interfere or act when their character and circumstances give them a reason; do not force a cameo.",
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
        value = context.get(key)
        if value:
            parts.append(json.dumps(value, ensure_ascii=False))
    return " ".join(parts).casefold()


def _foundation_pressure(source: Dict[str, Any], state: Dict[str, Any], context: Dict[str, Any], cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    foundation = _foundation(source)
    recent = _recent_text(context)
    hook_state = state.get("world", {}).get("foundation_hook_state", {}) if isinstance(state.get("world"), dict) else {}
    hook_state = hook_state if isinstance(hook_state, dict) else {}
    facts = {str(row.get("fact_id")): row for row in foundation.get("facts", []) if isinstance(row, dict) and row.get("fact_id")}
    candidates = []
    for hook in foundation.get("hooks", []) if isinstance(foundation.get("hooks"), list) else []:
        if not isinstance(hook, dict) or not hook.get("hook_id"):
            continue
        hid = str(hook["hook_id"])
        status = str(hook_state.get(hid, {}).get("status") or hook.get("status") or "latent").casefold()
        if status in {"resolved", "closed", "abandoned"}:
            continue
        linked = [facts.get(str(fid)) for fid in hook.get("fact_ids", []) if facts.get(str(fid))]
        snippets = [str(row.get("text") or "") for row in linked if isinstance(row, dict)]
        if snippets and all(text.casefold() in recent for text in snippets if len(text) < 120):
            continue
        candidates.append({
            "hook_id": hid,
            "fact_ids": [str(fid) for fid in hook.get("fact_ids", [])],
            "facts": snippets[:3],
            "condition": hook.get("condition") or hook.get("when"),
            "pillars": deepcopy(hook.get("pillar_ids", [])),
        })
        if len(candidates) >= 6:
            break

    legacy_cues = []
    if not foundation:
        for card in cards:
            cid = storage._card_id(card)
            for path, text in _flatten_driver_values(card):
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
        "instruction": "Setup facts are not decoration. Use eligible facts gradually when a natural memory, reaction, clue, conversation, consequence or plot opening makes them relevant. Do not dump several hooks at once and do not invent that a latent fact already happened in-session.",
    }


def _story_pillars(source: Dict[str, Any], state: Dict[str, Any], current_turn: int) -> Dict[str, Any]:
    foundation = _foundation(source)
    persisted = state.get("world", {}).get("story_pillars", {}) if isinstance(state.get("world"), dict) else {}
    persisted = persisted if isinstance(persisted, dict) else {}
    rows = []
    for raw in foundation.get("story_pillars", []) if isinstance(foundation.get("story_pillars"), list) else []:
        if not isinstance(raw, dict) or not raw.get("pillar_id"):
            continue
        pid = str(raw["pillar_id"])
        live = persisted.get(pid) if isinstance(persisted.get(pid), dict) else {}
        last = int(live.get("last_touched_turn", 0) or 0)
        age = max(0, current_turn - last)
        rows.append({
            "pillar_id": pid,
            "label": raw.get("label") or raw.get("name") or pid,
            "source_fact_ids": deepcopy(raw.get("source_fact_ids", [])),
            "turns_since_influence": age,
            "attention_due": current_turn >= 18 and age >= 18,
            "strongly_due": current_turn >= 36 and age >= 36,
        })

    if not rows:
        novel = source.get("novel") if isinstance(source.get("novel"), dict) else {}
        raw_genres = novel.get("genres") or novel.get("genre") or source.get("genre")
        if isinstance(raw_genres, str):
            raw_genres = [raw_genres]
        if isinstance(raw_genres, list):
            for index, genre in enumerate(raw_genres[:8]):
                rows.append({
                    "pillar_id": f"legacy_genre_{index+1}",
                    "label": str(genre),
                    "turns_since_influence": current_turn,
                    "attention_due": current_turn >= 18,
                    "strongly_due": current_turn >= 36,
                    "legacy_derived": True,
                })

    return {
        "mandatory_consideration": True,
        "pillars": rows,
        "instruction": "A story pillar must sometimes change what actually happens, not merely color descriptions. If one has been absent for a long time, prefer an existing causal hook/thread/world condition that lets it matter again. No quota and no random genre event.",
    }


def _social_world(state: Dict[str, Any]) -> Dict[str, Any]:
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    social = world.get("social") if isinstance(world.get("social"), dict) else {}
    signals = social.get("signals") if isinstance(social.get("signals"), dict) else {}
    recent = list(signals.values())[-12:] if signals else []
    return {
        "mandatory": True,
        "recent_social_signals": deepcopy(recent),
        "instruction": "Background people are people, not wallpaper. Salient appearance, loud/unusual behavior, status, conflict or repeated presence may be noticed, misread, discussed or remembered when the setting makes that plausible. A one-off extra may stay unnamed; if someone becomes important or recurring, persist them with character_upserts. Durable social fallout belongs in chronology.social_effect so it can affect later scenes. No telepathic rumors: information spreads only through real witnesses/channels.",
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
                "legacy_rule": "Existing saved legacy labels stay valid and are never renamed merely to fit the new vocabulary.",
                "numbers_are_behavioral": "Relationship values must change tone, initiative, distance, interpretation and choices when relevant; they are not decorative footer counters.",
                "opinion_rule": "current_opinion/beliefs may be wrong, biased or based on incomplete evidence. Keep the latest belief until something actually changes it.",
            },
            "npc_actor_frames": _actor_frames(context, state, cards),
            "foundation_pressure": _foundation_pressure(source, state, context, cards),
            "story_pillar_pressure": _story_pillars(source, state, current_turn),
            "social_reactivity": _social_world(state),
            "instruction": "Before writing an NPC, use their actor frame: character + wants + knowledge + relationship + current opinion + unresolved business. NPCs and the surrounding social world should create consequences and initiative instead of waiting for POV to animate them.",
        }
        persistence = context.get("persistence_contract") if isinstance(context.get("persistence_contract"), dict) else {}
        persistence["relationship_opinion"] = "When an NPC's actual opinion/beliefs/unresolved state changes, use relationship_updates with character_id plus opinion/current_dynamic and/or full beliefs_about_target/unresolved_between_them. Present NPC numeric dimensions still come from the visible footer."
        persistence["social_effect"] = "If a durable social reaction/rumor/reputation consequence is created, put social_effect inside the relevant chronology event."
        persistence["foundation_fact_ids"] = "When a foundation hook is genuinely used, attach foundation_fact_ids to the relevant chronology event or story thread anchor_facts so Railway can mark it touched."
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
    chronology = extracted.get("chronology") if isinstance(extracted.get("chronology"), list) else []
    for row in chronology:
        if not isinstance(row, dict):
            continue
        ids = row.get("foundation_fact_ids")
        if isinstance(ids, str):
            ids = [ids]
        if isinstance(ids, list):
            result.update(str(value) for value in ids if value)
    threads = extracted.get("story_thread_updates") if isinstance(extracted.get("story_thread_updates"), list) else []
    for row in threads:
        if not isinstance(row, dict):
            continue
        anchors = row.get("anchor_facts")
        if isinstance(anchors, str):
            anchors = [anchors]
        if isinstance(anchors, list):
            result.update(str(value) for value in anchors if value)
    return result


def _persist_world_effects(root, payload: Dict[str, Any], turn_number: int) -> None:
    extracted = payload.get("extracted") if isinstance(payload.get("extracted"), dict) else {}
    state = storage._read_json(root / "state.json", {})
    world = state.get("world") if isinstance(state.get("world"), dict) else {}
    world = deepcopy(world)
    changed = False

    social = world.get("social") if isinstance(world.get("social"), dict) else {}
    signals = social.get("signals") if isinstance(social.get("signals"), dict) else {}
    signals = deepcopy(signals)
    for index, row in enumerate(extracted.get("chronology", []) if isinstance(extracted.get("chronology"), list) else []):
        if not isinstance(row, dict) or not isinstance(row.get("social_effect"), dict):
            continue
        effect = deepcopy(row["social_effect"])
        effect["turn"] = turn_number
        signals[f"t{turn_number}_{index+1}"] = effect
        changed = True
    if changed:
        social = deepcopy(social)
        social["signals"] = signals
        world["social"] = social

    fact_ids = _extract_fact_ids(extracted)
    if fact_ids:
        source = storage._read_json(root / "source.json", {})
        foundation = _foundation(source)
        hook_state = world.get("foundation_hook_state") if isinstance(world.get("foundation_hook_state"), dict) else {}
        hook_state = deepcopy(hook_state)
        touched_pillars: set[str] = set()
        for hook in foundation.get("hooks", []) if isinstance(foundation.get("hooks"), list) else []:
            if not isinstance(hook, dict) or not hook.get("hook_id"):
                continue
            if not fact_ids.intersection(str(fid) for fid in hook.get("fact_ids", [])):
                continue
            hid = str(hook["hook_id"])
            row = hook_state.get(hid) if isinstance(hook_state.get(hid), dict) else {}
            row = deepcopy(row)
            row["status"] = "touched"
            row["last_touched_turn"] = turn_number
            row["touch_count"] = int(row.get("touch_count", 0) or 0) + 1
            hook_state[hid] = row
            touched_pillars.update(str(pid) for pid in hook.get("pillar_ids", []) if pid)
        world["foundation_hook_state"] = hook_state

        pillars = world.get("story_pillars") if isinstance(world.get("story_pillars"), dict) else {}
        pillars = deepcopy(pillars)
        for raw in foundation.get("story_pillars", []) if isinstance(foundation.get("story_pillars"), list) else []:
            if not isinstance(raw, dict) or not raw.get("pillar_id"):
                continue
            pid = str(raw["pillar_id"])
            sources = {str(fid) for fid in raw.get("source_fact_ids", []) if fid}
            if pid not in touched_pillars and not sources.intersection(fact_ids):
                continue
            row = pillars.get(pid) if isinstance(pillars.get(pid), dict) else {}
            row = deepcopy(row)
            row["last_touched_turn"] = turn_number
            pillars[pid] = row
        world["story_pillars"] = pillars
        changed = True

    if changed:
        state["world"] = world
        storage._write_json(root / "state.json", state)


def _prepare_turn(session_id: str, user_input: str) -> Dict[str, Any]:
    return _rewrite_packet(session_id, dict(_ORIGINAL_PREPARE(session_id, user_input)))


def _commit_turn(session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _validate_relationship_vocabulary(session_id, payload)
    prepared, metadata = _split_relationship_metadata(session_id, payload)
    result = _ORIGINAL_COMMIT(session_id, prepared)
    root = storage.SESSIONS_DIR / session_id
    turn_number = int(result.get("turn_number", 0) or 0)
    _apply_relationship_metadata(root, metadata, turn_number)
    _persist_world_effects(root, payload, turn_number)
    return result


def install() -> None:
    global _ORIGINAL_PREPARE, _ORIGINAL_COMMIT
    if _ORIGINAL_PREPARE is not None:
        return
    _ORIGINAL_PREPARE = session_runtime.prepare_turn_packet
    _ORIGINAL_COMMIT = session_runtime.commit_turn
    session_runtime.prepare_turn_packet = _prepare_turn
    session_runtime.commit_turn = _commit_turn
