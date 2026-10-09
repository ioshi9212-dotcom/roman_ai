from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List

from fastapi import HTTPException

from . import character_chunk_read, knowledge_firewall_runtime, private_knowledge_runtime, storage, writer_first_runtime


_VERSION = 2

_NUMBER_RE = re.compile(r"(?<!\w)\d{1,4}(?!\w)")
_KNOWLEDGE_ACTION_RE = re.compile(
    r"(?iu)\b(?:"
    r"провер\w*|обыск\w*|разыск\w*|отыск\w*|наш[её]л\w*|"
    r"ввел\w*|ввод\w*|набрал\w*|открыл\w*|отпер\w*|"
    r"достал\w*|вытащил\w*|направ\w*|подош\w*\s+прям\w*|"
    r"позвони\w*|написал\w*|переслал\w*|показал\w*|указал\w*|"
    r"спросил\w*\s+(?:про|о)\b"
    r")"
)
_WHISPER_RE = re.compile(
    r"(?iu)\b(?:шепот\w*|шёпот\w*|шепн\w*|шепч\w*|на\s+ухо|только\s+для)\b"
)
_INFERENCE_MARKERS = (
    "может",
    "возможно",
    "похоже",
    "кажется",
    "наверное",
    "вероятно",
    "думаю",
    "предполага",
    "если я правильно",
    "выходит",
)
_REMOTE_AUDIBLE_MARKERS = (
    "звон",
    "call",
    "voice",
    "голос",
    "видео",
    "video",
    "конферен",
)

_PRIVATE_MENTAL_MARKERS = (
    "подум",
    "вспомн",
    "решил",
    "решить",
    "знаю",
    "знать",
    "понял",
    "понять",
    "мыслен",
    "про себя",
    "намерен",
)

_OBSERVABLE_ACTION_RE = re.compile(
    r"(?iu)^\s*(?:"
    r"встат|сесть|подойт|отойт|пойт|уйт|вернут|взят|достат|убрат|положит|"
    r"открыт|закрыт|выключ|включ|посмотр|повернут|поднят|опустит|схват|"
    r"обнят|поцел|поглад|залез|выйт|зайт|пройт|наклон|присест|лечь|"
    r"смест|перехват|рассмотр|улыб|усмех|отвернут|продолж|остат|окаж|"
    r"кивнут|махнут|пожат|удар|брос|толк|коснут"
    r")\w*"
)

_GLOBAL_FORBIDDEN_SOURCES = [
    "character_cards[OTHER_CHARACTER_ID]",
    "character_memory[OTHER_CHARACTER_ID]",
    "own card branches marked unknown_to_self/hidden_from_self/not_known_to_self/known_to_self=false/author_only",
    "chronology_recent",
    "recent_turns",
    "continuity_turns",
    "scene_history",
    "novel/novel_lore/hidden_lore/world_canon",
    "future_guidance/story_direction/source_extra",
    "scene_state beyond actually perceived current physical facts",
    "location_context beyond actually perceived current physical facts",
    "canon_notes_context",
    "cast_registry",
    "npc_relationship_network",
    "npc_active_intents",
]

_PROTECTED_CONTEXT_PATHS = (
    "chronology_recent",
    "recent_turns",
    "continuity_turns",
    "scene_history",
    "novel",
    "novel_rules",
    "novel_lore",
    "hidden_lore",
    "world_canon",
    "future_guidance",
    "story_direction",
    "source_extra",
    "scene_state",
    "location_context",
    "canon_notes_context",
    "cast_registry",
    "npc_relationship_network",
    "npc_active_intents",
    "starting_state",
)

# Recent narrative and chronology provide continuity, not automatically private facts.
# Matching scattered common words against them is not evidence of a knowledge leak.
_NARRATIVE_CONTEXT_SOURCES = {
    "chronology_recent",
    "recent_turns",
    "continuity_turns",
    "scene_history",
}

_BROAD_DIRECTOR_SOURCES = {
    "novel",
    "novel_rules",
    "world_canon",
    "future_guidance",
    "story_direction",
    "source_extra",
    "scene_state",
    "location_context",
    "cast_registry",
    "npc_relationship_network",
    "npc_active_intents",
    "starting_state",
}


def _fact_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in (
        "text",
        "fact",
        "summary",
        "event",
        "description",
        "content",
        "memory",
        "note",
        "detail",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _iter_story_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)
        return
    if isinstance(value, dict):
        preferred = _fact_text(value)
        if preferred:
            yield preferred
            return
        for key, child in value.items():
            key_norm = str(key).casefold()
            if key_norm in knowledge_firewall_runtime._SELF_CONTROL_KEYS:
                continue
            yield from _iter_story_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from _iter_story_strings(child)


def _blocked_self_rows(value: Any, path: str = "") -> Iterable[Dict[str, str]]:
    if isinstance(value, dict):
        if knowledge_firewall_runtime._self_node_blocked(value):
            for text in _iter_story_strings(value):
                if text:
                    yield {
                        "source": f"self_hidden_card:{path or '<root>'}",
                        "text": text,
                    }
            return
        for key, child in value.items():
            key_norm = str(key).casefold()
            if key_norm in knowledge_firewall_runtime._SELF_CONTROL_KEYS:
                continue
            child_path = f"{path}.{key}" if path else str(key)
            yield from _blocked_self_rows(child, child_path)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _blocked_self_rows(child, f"{path}[{index}]")


def _self_card_text(card: Dict[str, Any] | None) -> str:
    if not isinstance(card, dict):
        return ""
    return "\n".join(knowledge_firewall_runtime._iter_self_card_texts(card))


def _memory_texts_from_bucket(bucket: Any) -> List[str]:
    source = bucket if isinstance(bucket, dict) else {}
    rows: List[str] = []

    journal = source.get("knowledge_journal")
    if isinstance(journal, str) and journal.strip():
        rows.append(journal.strip())
    elif isinstance(journal, list):
        rows.extend(
            str(row.get("text") or "").strip()
            for row in journal
            if isinstance(row, dict) and str(row.get("text") or "").strip()
        )

    knowledge = source.get("knowledge")
    if isinstance(knowledge, list):
        rows.extend(text for text in (_fact_text(row) for row in knowledge) if text)

    # A character also remembers their OWN experiences and conversations.
    # These are supporting recollections, not a license to invent someone else's secrets.
    for field in ("experiences", "dialogue_memory"):
        records = source.get(field)
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, str) and record.strip():
                rows.append(record.strip())
            elif isinstance(record, dict):
                details = [
                    str(record[key]).strip()
                    for key in (
                        "summary", "text", "fact", "event", "description",
                        "content", "memory", "note", "detail", "question", "answer",
                    )
                    if isinstance(record.get(key), str) and str(record[key]).strip()
                ]
                rows.extend(details)

    return list(dict.fromkeys(rows))


def _packet_context(root, payload: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]] | None:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet.get("chunks"):
        return None

    packet_id = str(payload.get("packet_id") or "").strip()
    if packet_id and packet_id != str(packet.get("packet_id") or ""):
        return None
    if str(payload.get("user_input") or "") != str(packet.get("user_input") or ""):
        return None

    try:
        value = json.loads("".join(str(chunk) for chunk in packet.get("chunks", [])))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return packet, value


def _loaded_bundle_chunks(
    session_id: str,
    packet: Dict[str, Any],
) -> Dict[str, List[str]]:
    reads = packet.get("character_bundle_reads")
    if not isinstance(reads, dict):
        return {}

    result: Dict[str, List[str]] = {}
    for character_id, row in reads.items():
        if not character_id or not isinstance(row, dict):
            continue
        read_id = str(row.get("read_id") or "").strip()
        indices = sorted({
            int(value)
            for value in row.get("read_chunks", [])
            if isinstance(value, int)
        })
        if not read_id or not indices:
            continue
        try:
            current_read_id, chunks = character_chunk_read._snapshot(
                session_id,
                str(character_id),
            )
        except (FileNotFoundError, KeyError):
            continue
        if current_read_id != read_id:
            continue

        visible = [
            str(chunks[index])
            for index in indices
            if 0 <= index < len(chunks)
        ]
        if visible:
            result[str(character_id)] = visible
    return result


def _context_cards(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    values = context.get("character_cards")
    if not isinstance(values, list):
        return []
    return [row for row in values if isinstance(row, dict)]


def _context_card_map(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        storage._card_id(card): card
        for card in _context_cards(context)
        if storage._card_id(card)
    }


def _context_memory_bucket(context: Dict[str, Any], character_id: str) -> Dict[str, Any]:
    memory = context.get("character_memory")
    if not isinstance(memory, dict):
        return {}
    bucket = memory.get(character_id)
    return bucket if isinstance(bucket, dict) else {}


def _state_sets(context: Dict[str, Any]) -> tuple[set[str], set[str], str, Dict[str, Any]]:
    state = context.get("scene_state") if isinstance(context.get("scene_state"), dict) else {}
    present = {str(value) for value in storage._present_character_ids(state) if value}
    remote = {str(value) for value in storage._remote_character_ids(state) if value}
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    return present, remote, pov_id, current


def _remote_channel(current: Dict[str, Any], character_id: str) -> str:
    channels = current.get("remote_channels") if isinstance(current.get("remote_channels"), dict) else {}
    return str(channels.get(character_id) or "")


def _remote_channel_audible(channel: str) -> bool:
    normalized = private_knowledge_runtime._norm(channel)
    return any(marker in normalized for marker in _REMOTE_AUDIBLE_MARKERS)


def _visible_card_text(card: Dict[str, Any] | None) -> str:
    if not isinstance(card, dict):
        return ""
    values: List[str] = []
    for key in ("appearance",):
        value = card.get(key)
        if value not in (None, "", [], {}):
            values.extend(_iter_story_strings(value))
    return "\n".join(values)


def _physical_perception_text(
    context: Dict[str, Any],
    character_id: str,
    card_map: Dict[str, Dict[str, Any]],
) -> str:
    present, _, _, current = _state_sets(context)
    if character_id not in present:
        return ""

    parts: List[str] = []
    for key in ("location", "location_id", "zone", "zone_id", "weather", "season"):
        value = current.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))

    positions = current.get("positions") if isinstance(current.get("positions"), dict) else {}
    scene_items = current.get("scene_items") if isinstance(current.get("scene_items"), dict) else {}
    parts.extend(_iter_story_strings(positions))
    parts.extend(_iter_story_strings(scene_items))

    scene_state = context.get("scene_state") if isinstance(context.get("scene_state"), dict) else {}
    runtime = scene_state.get("characters") if isinstance(scene_state.get("characters"), dict) else {}
    for cid in present:
        row = runtime.get(cid) if isinstance(runtime.get(cid), dict) else {}
        for key in ("clothing", "outfit", "hair", "activity", "zone", "position"):
            value = row.get(key)
            if value not in (None, "", [], {}):
                parts.extend(_iter_story_strings(value))
        if cid != character_id:
            visible = _visible_card_text(card_map.get(cid))
            if visible:
                parts.append(visible)

    location = context.get("location_context")
    if isinstance(location, dict):
        for key in ("name", "current_zone"):
            value = location.get(key)
            if value not in (None, "", [], {}):
                parts.extend(_iter_story_strings(value))

    return "\n".join(str(value) for value in parts if str(value).strip())


def _observable_stage_text(user_input: str) -> str:
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    visible: List[str] = []
    for stage in mapping.get("stage_directions", []):
        for part in re.split(r"(?<=[.!?;])\s+|\n+", str(stage or "")):
            clean = part.strip()
            if not clean:
                continue
            if (
                not private_knowledge_runtime._looks_like_action(clean)
                and _OBSERVABLE_ACTION_RE.search(clean) is None
            ):
                continue
            normalized = private_knowledge_runtime._norm(clean)
            if any(marker in normalized for marker in _PRIVATE_MENTAL_MARKERS):
                continue
            if (
                private_knowledge_runtime._COMMUNICATION_RE.search(clean)
                or private_knowledge_runtime._CHAT_RE.search(clean)
                or _WHISPER_RE.search(clean)
            ):
                continue
            visible.append(clean)
    return " ".join(visible)


def _current_input_access(
    user_input: str,
    context: Dict[str, Any],
    cards: List[Dict[str, Any]],
) -> Dict[str, str]:
    present, remote, pov_id, current = _state_sets(context)
    mapping = writer_first_runtime._parse_player_input(str(user_input or ""))
    public_text = " ".join(
        str(value)
        for value in mapping.get("spoken_segments", [])
        if str(value or "").strip()
    ).strip()

    result: Dict[str, List[str]] = {}
    observable_stage = _observable_stage_text(user_input)

    for cid in present:
        if cid == pov_id:
            continue
        if public_text:
            result.setdefault(cid, []).append(public_text)
        if observable_stage:
            result.setdefault(cid, []).append(observable_stage)

    if public_text:
        for cid in remote:
            if _remote_channel_audible(_remote_channel(current, cid)):
                result.setdefault(cid, []).append(public_text)

    for row in private_knowledge_runtime.extract_private_communications(str(user_input or ""), cards):
        cid = str(row.get("recipient_id") or "")
        payload = str(row.get("payload") or "").strip()
        if cid and payload:
            result.setdefault(cid, []).append(payload)

    return {
        cid: "\n".join(values)
        for cid, values in result.items()
        if values
    }


def _named_character_ids(text: str, cards: List[Dict[str, Any]], *, exclude_id: str = "") -> List[str]:
    normalized = private_knowledge_runtime._norm(text)
    exact, stems, _ = private_knowledge_runtime._alias_maps(cards)
    found: List[str] = []

    for alias, cid in exact.items():
        if not cid or cid == exclude_id or len(alias) < 2:
            continue
        if re.search(
            rf"(?<![a-zа-яё0-9_-]){re.escape(alias)}(?![a-zа-яё0-9_-])",
            normalized,
            flags=re.IGNORECASE,
        ):
            found.append(cid)

    for token in re.findall(r"(?iu)[a-zа-яё][a-zа-яё-]+", normalized):
        cid = private_knowledge_runtime._resolve_recipient(token, exact, stems)
        if cid and cid != exclude_id:
            found.append(cid)

    return list(dict.fromkeys(found))


def _speech_recipients(
    unit: Dict[str, Any],
    context: Dict[str, Any],
    cards: List[Dict[str, Any]],
) -> set[str]:
    cid = str(unit.get("character_id") or "")
    text = str(unit.get("text") or "")
    label = str(unit.get("speaker_label") or "")
    present, remote, pov_id, current = _state_sets(context)

    if _WHISPER_RE.search(text):
        targets = set(_named_character_ids(text, cards, exclude_id=cid))
        return {cid, *targets}

    marker_text = f"{label} {text}"
    explicit_remote = private_knowledge_runtime._REMOTE_MARKER_RE.search(marker_text) is not None

    if cid == pov_id:
        recipients = {pov_id}
        if explicit_remote:
            exact, stems, _ = private_knowledge_runtime._alias_maps(cards)
            target = private_knowledge_runtime._remote_target_from_label(
                label,
                exact=exact,
                stems=stems,
                exclude_id=pov_id,
            )
            if not target and len(remote) == 1:
                target = next(iter(remote))
            if target:
                recipients.add(target)
            return recipients

        recipients.update(present)
        for remote_id in remote:
            if _remote_channel_audible(_remote_channel(current, remote_id)):
                recipients.add(remote_id)
        return recipients

    if cid in remote or explicit_remote:
        return {cid, pov_id} if pov_id else {cid}

    if cid in present:
        return set(present)

    return {cid}


def _scene_units(scene_output: str, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scene = str(scene_output or "")
    result: List[Dict[str, Any]] = []

    speeches = private_knowledge_runtime._speaker_units(scene, cards)
    speech_positions = {int(row.get("position", -1)) for row in speeches}
    for row in speeches:
        result.append({**row, "kind": "speech"})

    main = scene
    for marker in ("\nЧто я могу сделать", "\nСостояние:", "\nОтношения:"):
        if marker in main:
            main = main.split(marker, 1)[0]

    offset = 0
    for paragraph in re.split(r"\n\s*\n", main):
        start = scene.find(paragraph, offset)
        if start < 0:
            start = offset
        offset = start + len(paragraph)

        clean = paragraph.strip()
        if not clean or start in speech_positions:
            continue
        if clean.startswith(("🎭", "🕒", "📍", "⚙️", "✦", "---", "[")):
            continue
        if re.match(r"^\s*\*\*[^*\n]+\*\*\s*[—-]\s*", clean):
            continue
        if _KNOWLEDGE_ACTION_RE.search(clean) is None:
            continue

        found = _named_character_ids(clean, cards)
        if len(found) != 1:
            continue
        result.append({
            "character_id": found[0],
            "speaker_label": "",
            "text": clean,
            "position": start,
            "kind": "action",
        })

    result.sort(key=lambda row: (int(row.get("position", 0)), 0 if row.get("kind") == "action" else 1))
    return result


def _numbers(text: str) -> set[str]:
    return {match.group(0) for match in _NUMBER_RE.finditer(str(text or ""))}


def _ordered_terms(text: str) -> List[str]:
    """Meaningful stems in their original order, unlike bag-of-words _terms."""
    result: List[str] = []
    for match in re.finditer(r"(?iu)[a-zа-яё][a-zа-яё-]{3,}", str(text or "")):
        terms = private_knowledge_runtime._terms(match.group(0))
        if terms:
            result.append(next(iter(terms)))
    return result


def _ordered_triples(text: str) -> set[tuple[str, str, str]]:
    terms = _ordered_terms(text)
    return {tuple(terms[i:i + 3]) for i in range(len(terms) - 2)}


def _looks_like_inference(text: str) -> bool:
    normalized = private_knowledge_runtime._norm(text)
    return "?" in str(text or "") or any(marker in normalized for marker in _INFERENCE_MARKERS)


def _protected_rows(
    context: Dict[str, Any],
    character_id: str,
    card_map: Dict[str, Dict[str, Any]],
    *,
    all_card_map: Dict[str, Dict[str, Any]],
    fallback_memory: Dict[str, Any],
    loaded_bundle_chunks: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    own_card = card_map.get(character_id) or all_card_map.get(character_id)
    rows.extend(_blocked_self_rows(own_card))

    for other_id, card in card_map.items():
        if other_id == character_id:
            continue
        for text in _iter_story_strings(card):
            rows.append({
                "source": f"character_card:{other_id}",
                "text": text,
            })

    memories = context.get("character_memory") if isinstance(context.get("character_memory"), dict) else {}
    for other_id, bucket in memories.items():
        other_id = str(other_id)
        if other_id == character_id:
            continue
        for text in _memory_texts_from_bucket(bucket):
            rows.append({
                "source": f"character_memory:{other_id}",
                "text": text,
            })

    for loaded_id, chunks in loaded_bundle_chunks.items():
        if loaded_id == character_id:
            continue
        for index, chunk in enumerate(chunks):
            rows.append({
                "source": f"loaded_bundle_chunk:{loaded_id}:{index}",
                "text": chunk,
            })

    for path in _PROTECTED_CONTEXT_PATHS:
        value = context.get(path)
        if value in (None, "", [], {}):
            continue
        for text in _iter_story_strings(value):
            rows.append({"source": path, "text": text})

    unique: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        text = str(row.get("text") or "").strip()
        source = str(row.get("source") or "unknown")
        if not text:
            continue
        key = (source, private_knowledge_runtime._norm(text))
        if key in seen:
            continue
        seen.add(key)
        unique.append({
            "source": source,
            "text": text,
            "terms": private_knowledge_runtime._terms(text),
            "numbers": _numbers(text),
            "source": source,
            "ordered_triples": _ordered_triples(text),
            "broad": row.get("broad_override") is True or source in _BROAD_DIRECTOR_SOURCES,
        })
    return unique


def _match_protected(
    used_terms: set[str],
    used_numbers: set[str],
    protected: Dict[str, Any],
    *,
    allowed_terms: set[str],
    allowed_numbers: set[str],
    inference_text: str,
    inference_triples: set[tuple[str, str, str]] | None = None,
) -> Dict[str, Any] | None:
    protected_terms = set(protected.get("terms") or [])
    protected_numbers = set(protected.get("numbers") or [])
    broad = protected.get("broad") is True

    raw_overlap = used_terms & protected_terms
    unknown_terms = raw_overlap - allowed_terms
    known_overlap = raw_overlap & allowed_terms
    unknown_numbers = (used_numbers & protected_numbers) - allowed_numbers

    # Coincidental bag-of-words matches are not evidence of an information
    # leak. A copied specific phrase, an exact unlearned number with context,
    # or a dense match to a short protected fact provides stronger evidence.
    triples = inference_triples if inference_triples is not None else _ordered_triples(inference_text)
    shared = triples & set(protected.get("ordered_triples") or ())
    copied = {
        term
        for triple in shared
        if len(set(triple) - allowed_terms) >= 2
        for term in triple
        if term not in allowed_terms
    }
    if copied:
        return {
            "terms": sorted(copied),
            "numbers": sorted(unknown_numbers),
            "reason": (
                "unsupported_narrative_detail_copy"
                if protected.get("source") in _NARRATIVE_CONTEXT_SOURCES
                else "unsupported_specificity"
            ),
        }

    if unknown_numbers and raw_overlap:
        return {
            "terms": sorted(unknown_terms),
            "numbers": sorted(unknown_numbers),
            "reason": "unsupported_exact_number",
        }

    if protected.get("source") in _NARRATIVE_CONTEXT_SOURCES:
        return None

    # Paraphrased, concrete short facts can still leak without word-for-word
    # copying. Four overlapping unknown content stems and a substantial share
    # of the protected fact are required. Two commonplace words never suffice.
    if (
        len(unknown_terms) >= 4
        and len(protected_terms) <= 24
        and len(unknown_terms) * 2 >= len(protected_terms)
    ):
        return {
            "terms": sorted(unknown_terms),
            "numbers": sorted(unknown_numbers),
            "reason": "unsupported_specificity",
        }

    return None

def _authorized_base_text(
    context: Dict[str, Any],
    character_id: str,
    *,
    context_card_map: Dict[str, Dict[str, Any]],
    all_card_map: Dict[str, Dict[str, Any]],
    fallback_memory: Dict[str, Any],
) -> str:
    card = context_card_map.get(character_id) or all_card_map.get(character_id)
    pieces = [_self_card_text(card)]

    bucket = _context_memory_bucket(context, character_id)
    if bucket:
        pieces.extend(_memory_texts_from_bucket(bucket))
    else:
        characters = fallback_memory.get("characters") if isinstance(fallback_memory.get("characters"), dict) else {}
        pieces.extend(_memory_texts_from_bucket(characters.get(character_id, {})))

    return "\n".join(piece for piece in pieces if piece)


def build_boundaries(
    context: Dict[str, Any],
    character_ids: List[str],
    *,
    pov_id: str = "",
) -> Dict[str, Any]:
    present, remote, _, current = _state_sets(context)
    characters: Dict[str, Any] = {}

    for character_id in character_ids:
        cid = str(character_id)
        if not cid or cid == pov_id:
            continue
        access = "offscreen"
        if cid in present:
            access = "physical"
        elif cid in remote:
            channel = _remote_channel(current, cid)
            access = f"remote:{channel or 'active'}"

        characters[cid] = {
            "may_know": [
                f"character_cards[{cid}] self-known branches only",
                f"character_memory[{cid}].knowledge",
                f"character_memory[{cid}].knowledge_journal",
                "current-turn information actually perceived/heard/read/received by this character",
            ],
            "must_not_know": "global_must_not_know",
            "current_access": access,
        }

    return {
        "version": _VERSION,
        "mandatory": True,
        "epistemic_authority": (
            "Own self-known profile + own factual knowledge/journal + current-turn information actually perceived or received."
        ),
        "global_must_not_know": list(_GLOBAL_FORBIDDEN_SOURCES),
        "previous_scene_rule": (
            "Previous scene_output/recent/chronology are narrative continuity only. "
            "They never grant personal knowledge by themselves."
        ),
        "inference_rule": (
            "An inference must be visibly uncertain and use premises already available to that NPC. "
            "It may not introduce an unavailable exact number, hidden object/detail, or hidden relation as established fact."
        ),
        "characters": characters,
    }


def validate_scene_output(session_id: str, payload: Dict[str, Any]) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    packet_context = _packet_context(root, payload)
    if packet_context is None:
        # Packet identity/completeness remains owned by the existing turn pipeline.
        return
    packet, context = packet_context
    loaded_bundle_chunks = _loaded_bundle_chunks(session_id, packet)

    source = storage._read_json(root / "source.json", {})
    all_cards = storage._load_cards(root, source)
    all_card_map = {
        storage._card_id(card): card
        for card in all_cards
        if storage._card_id(card)
    }
    context_card_map = _context_card_map(context)

    present, _, pov_id, _ = _state_sets(context)
    if not pov_id:
        state = storage._read_json(root / "state.json", {})
        pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
        pov_id = str(pov.get("character_id") or "")

    user_input = str(payload.get("user_input") or "")
    current_input = _current_input_access(
        user_input,
        context,
        all_cards,
    )
    transfer_allowed_terms: Dict[str, set[str]] = {}
    if private_knowledge_runtime._TRANSFER_RE.search(user_input):
        private_records = private_knowledge_runtime._private_records(root, all_cards, user_input)
        transfer_sources = private_knowledge_runtime._current_private_transfer_sources(
            user_input,
            all_cards,
            private_records,
            pov_id=pov_id,
        )
        transfer_allowed_terms = private_knowledge_runtime._current_transfer_allowed_terms(
            payload,
            private_records,
            transfer_sources,
            pov_id=pov_id,
        )
    units = _scene_units(str(payload.get("scene_output") or ""), all_cards)
    packet_memory = context.get("character_memory") if isinstance(context.get("character_memory"), dict) else {}
    needs_fallback_memory = any(
        str(unit.get("character_id") or "") not in {"", pov_id}
        and str(unit.get("character_id") or "") not in packet_memory
        for unit in units
    )
    fallback_memory = (
        storage._normalise_memory(storage._read_json(root / "memory.json", {}))
        if needs_fallback_memory
        else {"characters": {}}
    )

    protected_cache: Dict[str, List[Dict[str, Any]]] = {}
    base_cache: Dict[str, str] = {}
    perception_cache: Dict[str, str] = {}
    earlier_access: Dict[str, List[str]] = {}

    for unit in units:
        cid = str(unit.get("character_id") or "")
        text = str(unit.get("text") or "").strip()
        kind = str(unit.get("kind") or "speech")
        if not cid or not text:
            continue

        if cid != pov_id:
            # setdefault evaluates its default argument even on a cache hit.
            # Build each NPC's stable knowledge/perception context only once.
            if cid not in base_cache:
                base_cache[cid] = _authorized_base_text(
                    context,
                    cid,
                    context_card_map=context_card_map,
                    all_card_map=all_card_map,
                    fallback_memory=fallback_memory,
                )
            base_text = base_cache[cid]
            if cid not in perception_cache:
                perception_cache[cid] = _physical_perception_text(
                    context, cid, context_card_map,
                )
            perception = perception_cache[cid]
            allowed_text = "\n".join([
                base_text,
                perception,
                current_input.get(cid, ""),
                *earlier_access.get(cid, []),
            ])
            allowed_terms = private_knowledge_runtime._terms(allowed_text)
            allowed_terms.update(transfer_allowed_terms.get(cid, set()))
            allowed_numbers = _numbers(allowed_text)

            unsupported: List[Dict[str, Any]] = []
            leaked_terms: set[str] = set()
            leaked_numbers: set[str] = set()
            used_terms = private_knowledge_runtime._terms(text)
            used_numbers = _numbers(text)
            used_triples = _ordered_triples(text)

            if cid not in protected_cache:
                protected_cache[cid] = _protected_rows(
                    context,
                    cid,
                    context_card_map,
                    all_card_map=all_card_map,
                    fallback_memory=fallback_memory,
                    loaded_bundle_chunks=loaded_bundle_chunks,
                )
            for row in protected_cache[cid]:
                protected_text = str(row.get("text") or "").strip()
                match = _match_protected(
                    used_terms,
                    used_numbers,
                    row,
                    allowed_terms=allowed_terms,
                    allowed_numbers=allowed_numbers,
                    inference_text=text,
                    inference_triples=used_triples,
                )
                if match is None:
                    continue

                leaked_terms.update(match.get("terms") or [])
                leaked_numbers.update(match.get("numbers") or [])
                unsupported.append({
                    "source": str(row.get("source") or "unknown"),
                    "fact": protected_text[:320],
                    "reason": str(match.get("reason") or "unsupported"),
                })
                if len(unsupported) >= 3:
                    break

            if unsupported:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SCENE_NPC_KNOWLEDGE_LEAK",
                        "character_id": cid,
                        "unit_kind": kind,
                        "unsupported_text": text,
                        "unsupported_facts": unsupported,
                        "leaked_terms": sorted(leaked_terms),
                        "leaked_numbers": sorted(leaked_numbers),
                        "instruction": (
                            "Rewrite only the unsupported NPC speech/action and retry the same pending packet_id. "
                            "Chronology/recent/previous scene_output, hidden/director context, another character's card or memory, "
                            "and hidden branches of the NPC's own card are not epistemic authority."
                        ),
                    },
                )

        if kind == "speech":
            recipients = _speech_recipients(unit, context, all_cards)
            for recipient_id in recipients:
                if recipient_id:
                    earlier_access.setdefault(recipient_id, []).append(text)
        elif cid in present:
            for recipient_id in present:
                earlier_access.setdefault(recipient_id, []).append(text)
