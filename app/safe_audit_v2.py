"""Opt-in, non-destructive audit v2. Existing sessions keep their old behavior.

Only sessions with meta.safe_audit_v2=True use this transport. Raw source
records remain in their original files; the writer receives the active,
compacted representation after the audit has verified it.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def enabled(meta: Any) -> bool:
    return isinstance(meta, dict) and meta.get("safe_audit_v2") is True


def active_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    """Keep active records for an opted-in writer without modifying storage."""
    result = deepcopy(bucket)
    for key in ("knowledge_journal", "knowledge", "experiences", "dialogue_memory"):
        values = result.get(key)
        if isinstance(values, list):
            result[key] = [
                row for row in values
                if not isinstance(row, dict) or not row.get("superseded_by")
            ]
    return result


def writer_memory_document(display: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Hide superseded source rows only if their live canonical replacement exists.

    Matching uses _runtime.records pointers rather than dates or text guesses.
    Preserve ordering, all other fields and per-character knowledge boundaries.
    The immutable raw evidence remains in the stored document and scene archive.
    """
    result = deepcopy(display)
    runtime = stored.get("_runtime") if isinstance(stored.get("_runtime"), dict) else {}
    records = runtime.get("records", [])
    records = records if isinstance(records, list) else []
    active_replacements: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        fields = record.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        kind = str(record.get("kind") or "")
        id_key = {
            "knowledge_journal": "entry_id",
            "knowledge": "fact_id",
            "experiences": "event_id",
            "dialogue_memory": "topic_id",
        }.get(kind)
        if (
            id_key and fields.get("canonical_compaction") is True
            and not fields.get("superseded_by") and fields.get(id_key)
        ):
            active_replacements.add((kind, str(fields[id_key])))

    remove: set[tuple[str, str, int]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        fields = record.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        kind = str(record.get("kind") or "")
        replacement = str(fields.get("superseded_by") or "")
        pointer = record.get("pointer")
        if (
            replacement and (kind, replacement) in active_replacements
            and isinstance(pointer, list) and len(pointer) == 3
        ):
            try:
                remove.add((str(pointer[0]), str(pointer[1]), int(pointer[2])))
            except (TypeError, ValueError):
                continue

    for section in ("знания", "личные_события"):
        dates = result.get(section)
        if not isinstance(dates, dict):
            continue
        for day, rows in list(dates.items()):
            if isinstance(rows, list):
                dates[day] = [
                    value for index, value in enumerate(rows)
                    if (section, str(day), index) not in remove
                ]
                if not dates[day]:
                    dates.pop(day)
    return result


def macro_knowledge_review(memory: dict[str, Any]) -> dict[str, Any]:
    """Entire active personal knowledge for every owner, never NPC cross-knowledge."""
    characters = memory.get("characters") if isinstance(memory, dict) else {}
    characters = characters if isinstance(characters, dict) else {}
    return {
        str(character_id): {
            key: deepcopy(active_bucket(bucket).get(key, []))
            for key in ("knowledge_journal", "knowledge", "experiences", "dialogue_memory")
        }
        for character_id, bucket in characters.items()
        if isinstance(bucket, dict)
    }


def validate_macro_review(repairs: dict[str, Any], known_ids: set[str]) -> None:
    """Fail closed: don't declare 60-turn audit completed without each owner reviewed."""
    review = repairs.get("macro_knowledge_review")
    if not isinstance(review, dict):
        raise RuntimeError("MACRO_KNOWLEDGE_REVIEW_REQUIRED")
    ids = review.get("reviewed_character_ids")
    if not isinstance(ids, list) or set(ids) != known_ids or len(ids) != len(known_ids):
        raise RuntimeError("MACRO_KNOWLEDGE_REVIEW_INCOMPLETE")
    if not isinstance(repairs.get("memory_compactions"), list):
        raise RuntimeError("MACRO_MEMORY_COMPACTION_REVIEW_REQUIRED")


def verify_critical_literals(source_rows: list[dict[str, Any]], summary: str) -> None:
    """Conservatively keep exact numbers and timestamps when shortening memories.

    Semantic completeness still needs author verification; this only protects
    easy-to-destroy consequential literals such as dates, ages, prices and times.
    """
    import re
    source = " ".join(
        str(row.get(key) or "")
        for row in source_rows
        for key in ("fact", "text", "summary")
    )
    literals = set(re.findall(r"(?<!\w)\d+(?:[.:/-]\d+)*(?!\w)", source))
    for literal in literals:
        if literal not in summary:
            raise RuntimeError("MEMORY_COMPACTION_CRITICAL_LITERAL_LOST")
