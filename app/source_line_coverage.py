"""Read-only, deterministic indicator of literal RAW coverage in saved canon.

This is NOT a semantic audit: a matched line may be misattributed, and a
paraphrased line can convey facts without matching. The diagnostic deliberately
reports them as 'requires human/GPT review', never 'missing canon' or 'verified'.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


_IGNORED = frozenset({"generated_details", "сгенерированные_дополнения", "generated_additions", "raw_text"})
_MARKER = re.compile(r"^===== (?:RAW|END RAW) [A-Za-z0-9_-]+(?: [0-9a-f]{64})? =====$")


def _field_texts(value: Any):
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if str(key) not in _IGNORED and not str(key).startswith("_"):
                yield from _field_texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _field_texts(child)


def source_line_coverage(
    raw_blocks: list[dict[str, Any]],
    canon_sections: dict[str, Any],
    *,
    sample_limit: int = 40,
) -> dict[str, Any]:
    """Report exact text lines represented somewhere in author-facing fields.

    Generated details, the raw intake itself, and private technical metadata
    do not count. Keep only the largest full source when smaller blocks are
    exact prefixes/substrings of it, to avoid double-counting a repaired upload.
    """
    sources = [
        (str(row.get("block_id") or "raw"), str(row.get("raw_text") or ""))
        for row in raw_blocks if isinstance(row, dict) and isinstance(row.get("raw_text"), str)
        and row.get("raw_text")
    ]
    if not sources:
        return {"available": False, "reason": "RAW_NOT_SAVED"}
    largest = max(sources, key=lambda row: len(row[1]))
    sources = [
        entry for entry in sources
        if entry == largest or entry[1] not in largest[1]
    ]
    saved = "\x00".join(_field_texts(canon_sections))
    eligible = matched = 0
    missing = []
    total_lines = 0
    for block_id, raw in sources:
        for number, line in enumerate(raw.splitlines(), 1):
            total_lines += 1
            if len(line.strip()) < 16 or _MARKER.fullmatch(line.strip()):
                continue
            eligible += 1
            if line in saved:
                matched += 1
            elif len(missing) < max(0, min(sample_limit, 100)):
                missing.append({
                    "block_id": block_id,
                    "line": number,
                    "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                })
    return {
        "available": True,
        "raw_blocks_considered": len(sources),
        "total_raw_lines": total_lines,
        "eligible_source_lines": eligible,
        "literal_lines_found": matched,
        "literal_lines_not_found": eligible - matched,
        "literal_line_match_percent": round(100 * matched / eligible, 2) if eligible else None,
        "unmatched_line_locations": missing,
        "sample_truncated": eligible - matched > len(missing),
        "interpretation": (
            "This is a literal-string diagnostic, NOT proof of complete source "
            "coverage, correct speaker attribution or character knowledge. "
            "Unmatched source lines require review against RAW before claiming "
            "a lossless profile; short headings (<16 characters) are excluded."
        ),
    }
