from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from .performance_metrics import timed

from .character_access import get_character_bundle
from . import personal_memory_transport, relationship_file_runtime, storage, session_runtime
from .scene_compaction_runtime import active_memory_records, complete_knowledge_records
from .transactional_storage import session_transaction


CHARACTER_CHUNK_CHARS = 12000
CHARACTER_WORKING_KNOWLEDGE = 18
CHARACTER_WORKING_EXPERIENCES = 12
CHARACTER_WORKING_DIALOGUE = 12
CHARACTER_HISTORICAL_CATALOG = 8
CHARACTER_MEMORY_TEXT_CHARS = 900
MAX_INTENT_SOURCE_FACTS = 12
_ORIGINAL_INJECT = None


def _bundle_knowledge_scope(character_id: str) -> Dict[str, Any]:
    return {
        "character_id": character_id,
        "own_card_is_self_known_except_explicit_hidden_branches": True,
        "allowed_sources": [
            "self-known parts of own card/self biography",
            "personal_memory",
            "real perception/contact",
            "what another character actually told them",
            "inference from already known facts",
        ],
        "forbidden_sources": [
            "other character cards",
            "other character memories",
            "chronology as automatic personal knowledge",
            "hidden lore",
            "future guidance",
            "POV private thoughts",
            "own card branches marked unknown_to_self/hidden_from_self/not_known_to_self/known_to_self=false/author_only",
        ],
        "instruction": "Персонаж знает self-known части своей карточки и свою память. Явно скрытые от него ветки собственной карточки и чужой канон знанием не становятся.",
    }


def _turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        return int(item.get("learned_turn") or item.get("turn_number") or item.get("turn") or 0)
    except (TypeError, ValueError):
        return 0


def _recency_turn(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    try:
        if item.get("last_learned_turn") not in (None, ""):
            return int(item["last_learned_turn"])
    except (TypeError, ValueError):
        pass
    return _turn(item)


def _id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("fact_id", "event_id", "topic_id", "id"):
        if item.get(key):
            return str(item[key])
    return None


def _summary(item: Dict[str, Any]) -> str:
    for key in ("fact", "summary", "event", "description", "text", "content", "topic", "memory", "note", "detail"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:220]
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))[:220]


def _bound_memory_value(value: Any) -> Any:
    """Bound writer-facing memory prose without touching identifiers or persistent storage."""
    if isinstance(value, str):
        if len(value) <= CHARACTER_MEMORY_TEXT_CHARS:
            return value
        return value[:CHARACTER_MEMORY_TEXT_CHARS] + "…[full memory text remains in persistent storage]"
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            # IDs must remain exact so intent source references and audit provenance still work.
            if key in {"fact_id", "event_id", "topic_id", "id", "character_id", "source_turn"}:
                result[key] = deepcopy(item)
            else:
                result[key] = _bound_memory_value(item)
        return result
    if isinstance(value, list):
        return [_bound_memory_value(item) for item in value]
    return deepcopy(value)


def _tail(values: Any, limit: int) -> List[Dict[str, Any]]:
    rows = active_memory_records(values)
    rows.sort(key=_recency_turn)
    return rows[-limit:]


def _intent_source_ids(bundle: Dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for intent in bundle.get("active_intents", []) if isinstance(bundle.get("active_intents"), list) else []:
        if not isinstance(intent, dict):
            continue
        values = intent.get("source_fact_ids")
        if not isinstance(values, list):
            continue
        for value in values[:MAX_INTENT_SOURCE_FACTS]:
            if value not in (None, ""):
                result.add(str(value))
    return result


def _working_memory(
    bundle: Dict[str, Any],
    *,
    character_id: str,
    cards: List[Dict[str, Any]],
) -> Dict[str, Any]:
    memory = bundle.get("personal_memory") if isinstance(bundle.get("personal_memory"), dict) else {}
    all_knowledge = complete_knowledge_records(memory.get("knowledge"))

    # Knowledge is factual authority for this character. Do not replace old facts
    # with a tiny historical catalog: the bundle is chunked, so all active facts can
    # be transported safely.
    knowledge = deepcopy(all_knowledge)
    # V5 journals are personal knowledge too. Only this owner's bundle may
    # transport these records; other cast members never receive them.
    journal = deepcopy(memory.get("knowledge_journal", []))
    if not isinstance(journal, list):
        journal = []
    journal = [row for row in journal if isinstance(row, (dict, str))]

    experiences = _tail(memory.get("experiences"), CHARACTER_WORKING_EXPERIENCES)
    dialogue = personal_memory_transport.personal_dialogue_rows(
        _tail(memory.get("dialogue_memory"), CHARACTER_WORKING_DIALOGUE),
        owner_id=character_id,
        cards=cards,
    )
    return {
        "knowledge": knowledge,
        "knowledge_journal": journal,
        "experiences": _bound_memory_value(experiences),
        "dialogue_memory": _bound_memory_value(dialogue),
        "historical_knowledge_catalog": [],
        "knowledge_complete_in_transport": True,
        "knowledge_text_not_truncated_in_transport": True,
        "persistent_counts": {
            "knowledge": len(memory.get("knowledge", [])) if isinstance(memory.get("knowledge"), list) else 0,
            "knowledge_journal": len(journal),
            "experiences": len(memory.get("experiences", [])) if isinstance(memory.get("experiences"), list) else 0,
            "dialogue_memory": len(memory.get("dialogue_memory", [])) if isinstance(memory.get("dialogue_memory"), list) else 0,
        },
        "canonical_active_counts": {
            "knowledge": len(all_knowledge),
            "experiences": len(active_memory_records(memory.get("experiences"))),
            "dialogue_memory": len(active_memory_records(memory.get("dialogue_memory"))),
        },
        "older_history_available": bool(
            len(active_memory_records(memory.get("experiences"))) > len(experiences)
            or len(active_memory_records(memory.get("dialogue_memory"))) > len(dialogue)
        ),
        "oversized_record_text_bounded_in_transport": True,
        "memory_text_chars_max": CHARACTER_MEMORY_TEXT_CHARS,
    }


def _participation_bundle(session_id: str, character_id: str) -> Dict[str, Any]:
    full = get_character_bundle(session_id, character_id)
    root = storage.SESSIONS_DIR / session_id
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    relationship_store = relationship_file_runtime.load(
        root,
        cards=cards,
        state=state,
        pov_id=str(pov.get("character_id") or ""),
    )
    return {
        "knowledge_scope": _bundle_knowledge_scope(character_id),
        "character_id": character_id,
        "card": deepcopy(full.get("card", {})),
        "current_state": deepcopy(full.get("current_state", {})),
        "pov_familiarity": deepcopy(full.get("pov_familiarity")),
        "personal_memory": _working_memory(full, character_id=character_id, cards=cards),
        "relationship_to_pov": deepcopy(full.get("relationship_to_pov")),
        "npc_relationships_director_only": relationship_file_runtime.outgoing_npc_relations(relationship_store, character_id),
        "active_intents": deepcopy(full.get("active_intents", [])),
        "working_bundle": True,
        "persistent_lifetime_memory_complete": True,
        "instruction": "Собственная card — self-known биография кроме явно hidden/unknown-to-self веток. personal_memory.knowledge/knowledge_journal — выученные факты. relationship_to_pov и npc_relationships_director_only берутся только из relationships.json. npc_relationships_director_only задаёт режиссёрскую динамику владельца связи, но НЕ является личным factual knowledge и не даёт неизвестных фактов о target. Частичный факт остаётся частичным: неизвестные время/место/человек/причина не достраиваются вероятными значениями; нужную деталь уточняют или оставляют догадкой до подтверждения. experiences/dialogue могут быть bounded.",
    }


_BUNDLE_DEPENDENCIES = (
    "source.json", "characters.json", "state.json", "memory.json",
    "meta.json", "relationships.json",
)
_BUNDLE_CACHE_VERSION = 1


def _bundle_signature(root) -> Dict[str, Any]:
    # Stat-only validation avoids rebuilding character dossiers on every chunk.
    # Source of truth remains the canonical files, never this derived cache.
    return {
        name: list(identity) if (identity := storage._file_identity(root / name)) is not None else None
        for name in _BUNDLE_DEPENDENCIES
    }


def _snapshot(session_id: str, character_id: str) -> tuple[str, List[str]]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)
    with session_transaction(root):
        key = hashlib.sha256(str(character_id).encode("utf-8")).hexdigest()[:32]
        cache_path = root / f"character_bundle_cache_{key}.json"
        signature = _bundle_signature(root)
        cache = storage._read_optional_dict(cache_path)
        if (
            cache.get("version") == _BUNDLE_CACHE_VERSION
            and cache.get("character_id") == character_id
            and cache.get("signature") == signature
            and isinstance(cache.get("chunks"), list)
            and cache["chunks"]
            and all(isinstance(c, str) for c in cache["chunks"])
            and isinstance(cache.get("read_id"), str)
        ):
            return cache["read_id"], cache["chunks"]

        with timed("npc_bundle", "rebuild"):
            bundle = _participation_bundle(session_id, character_id)
            text = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
            read_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
            chunks = [
                text[i:i + CHARACTER_CHUNK_CHARS]
                for i in range(0, len(text), CHARACTER_CHUNK_CHARS)
            ] or ["{}"]
        storage._write_json(cache_path, {
            "version": _BUNDLE_CACHE_VERSION,
            "character_id": character_id,
            "signature": _bundle_signature(root),
            "read_id": read_id,
            "chunks": chunks,
        })
        return read_id, chunks


def _record_pending_bundle_read(
    session_id: str,
    character_id: str,
    read_id: str,
    chunk_index: int,
    chunk_count: int,
) -> None:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        return
    with session_transaction(root):
        # The same compact packet index used for scene chunks also identifies
        # bundle-read progress. Do not reserialize the full turn packet here.
        index = storage._fast_packet_index(root)
        if index:
            packet_id = str(index["packet_id"])
            packet_count = int(index["chunk_count"])
            digest = str(index["content_digest"])
            fallback_reads = {}
        else:
            packet = storage._read_json(root / "turn_packet.json", {})
            if not isinstance(packet, dict) or not packet.get("packet_id") or not packet.get("chunks"):
                return
            packet_id = str(packet["packet_id"])
            packet_count = len(packet["chunks"])
            digest = storage._packet_content_digest(packet)
            fallback_reads = packet.get("character_bundle_reads", {})

        path = root / storage.TURN_PACKET_BUNDLE_PROGRESS
        progress = storage._read_optional_dict(path)
        if (
            progress.get("packet_id") == packet_id
            and progress.get("chunk_count") == packet_count
            and progress.get("content_digest") == digest
        ):
            reads = progress.get("character_bundle_reads")
            reads = deepcopy(reads) if isinstance(reads, dict) else {}
        else:
            reads = deepcopy(fallback_reads) if isinstance(fallback_reads, dict) else {}

        row = reads.get(character_id)
        if not isinstance(row, dict) or str(row.get("read_id") or "") != read_id:
            row = {"read_id": read_id, "chunk_count": int(chunk_count), "read_chunks": []}
        seen = {
            int(value)
            for value in row.get("read_chunks", [])
            if isinstance(value, int) and 0 <= value < int(chunk_count)
        }
        if int(chunk_index) in seen and row.get("chunk_count") == int(chunk_count):
            return
        seen.add(int(chunk_index))
        row["read_chunks"] = sorted(seen)
        row["chunk_count"] = int(chunk_count)
        reads[str(character_id)] = row
        storage._write_json(path, {
            "packet_id": packet_id,
            "chunk_count": packet_count,
            "content_digest": digest,
            "character_bundle_reads": reads,
        })


def prepare_character_bundle_read(session_id: str, character_id: str) -> Dict[str, Any]:
    with timed("npc_bundle", "prepare"):
        read_id, chunks = _snapshot(session_id, character_id)
    if chunks:
        _record_pending_bundle_read(session_id, character_id, read_id, 0, len(chunks))
    result: Dict[str, Any] = {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_count": len(chunks),
        "chunk_chars_max": CHARACTER_CHUNK_CHARS,
        "first_chunk_included": bool(chunks),
        "next_chunk_index": 1 if len(chunks) > 1 else None,
        "instruction": "NPC уже выбран для участия. Chunk 0 включён; прочитай остальные chunks до его содержательной реплики/действия. Это чтение проверяет знания, а не решает, может ли NPC появиться.",
    }
    if chunks:
        result["chunk_index"] = 0
        result["content"] = chunks[0]
        result["all_chunks_read"] = len(chunks) == 1
    return result


def get_character_bundle_chunk(
    session_id: str,
    character_id: str,
    read_id: str,
    chunk_index: int,
) -> Dict[str, Any]:
    with timed("npc_bundle", "read_chunk"):
        current_read_id, chunks = _snapshot(session_id, character_id)
    if current_read_id != read_id:
        raise PermissionError("STALE_CHARACTER_READ")
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError(chunk_index)
    _record_pending_bundle_read(
        session_id,
        character_id,
        read_id,
        chunk_index,
        len(chunks),
    )
    return {
        "session_id": session_id,
        "character_id": character_id,
        "read_id": read_id,
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "next_chunk_index": None if chunk_index + 1 >= len(chunks) else chunk_index + 1,
    }


def install() -> None:
    """Keep dormant dossiers out of normal packets while advertising the safe retrieval path."""
    global _ORIGINAL_INJECT
    if _ORIGINAL_INJECT is not None:
        return
    from . import session_runtime

    _ORIGINAL_INJECT = session_runtime.inject_required_turn_context

    def wrapped(context, cards, state):
        result = _ORIGINAL_INJECT(context, cards, state)
        result["character_context_instruction"] = "Offscreen NPC выбирается из активного cast_registry без отдельного допуска от intent/thread. После выбора прочитай prepareCharacterBundleRead и все chunks до содержательной реплики/действия; bundle проверяет знания, а не право появиться."
        contract = result.get("working_context_contract") if isinstance(result.get("working_context_contract"), dict) else {}
        contract["dormant_character_retrieval"] = "bounded_chunked_when_relevant"
        contract["remote_communication_requires_loaded_dossier"] = True
        contract["character_read_first_chunk_inline"] = True
        contract["direct_character_bundle_action_allowed"] = False
        result["working_context_contract"] = contract
        return result

    session_runtime.inject_required_turn_context = wrapped
