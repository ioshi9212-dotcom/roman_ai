"""Source-anchored canon fields. Never silently rewrite a locked author quote.

A quote is accepted only when the exact, byte-for-byte wording occurs in the
original RAW and is also the entire value at the declared JSON Pointer.
Uncited fields are NOT certified by this limited verifier; AI additions must
be kept in a clearly separate field and never asserted to be author canon.
"""
from __future__ import annotations

from typing import Any


def _pointer(document: Any, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 300:
        raise ValueError("CANON_QUOTE_PATH_INVALID")
    item = document
    for raw_segment in path[1:].split("/"):
        # JSON Pointer escapes are literal; reject malformed escapes.
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if "~" in segment and "~" not in raw_segment:
            pass
        if isinstance(item, dict) and segment in item:
            item = item[segment]
        elif isinstance(item, list) and segment.isdecimal() and int(segment) < len(item):
            item = item[int(segment)]
        else:
            raise ValueError("CANON_QUOTE_PATH_INVALID")
    return item


def verify_verbatim_fields(
    document: Any,
    submitted: dict[str, str] | None,
    original_raw: str,
    *,
    previously_locked: dict[str, str] | None = None,
) -> dict[str, str]:
    previous = dict(previously_locked or {})
    provided = dict(submitted or {})
    if len(previous) + len(provided) > 200:
        raise ValueError("CANON_QUOTE_TOO_MANY")
    if not isinstance(original_raw, str) or not original_raw:
        if provided or previous:
            raise ValueError("CANON_ORIGINAL_RAW_REQUIRED")
        return {}
    for path, quote in provided.items():
        if not isinstance(path, str) or not isinstance(quote, str) or not 1 <= len(quote) <= 12000:
            raise ValueError("CANON_QUOTE_INVALID")
        if quote not in original_raw:
            raise ValueError("CANON_QUOTE_MISSING_FROM_RAW")
        if _pointer(document, path) != quote:
            raise ValueError("CANON_QUOTE_NOT_VERBATIM")
    for path, quote in previous.items():
        if path in provided and provided[path] != quote:
            raise ValueError("CANON_LOCK_CHANGED")
        try:
            current = _pointer(document, path)
        except ValueError:
            raise ValueError("CANON_LOCK_CHANGED") from None
        if current != quote:
            raise ValueError("CANON_LOCK_CHANGED")
    return {**previous, **provided}
