from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict

from . import novel_drafts


_ORIGINAL_NORMALISE = None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _slug(value: Any, fallback: str) -> str:
    text = _text(value).casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or fallback


def _as_list(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if isinstance(item, bool):
                if item:
                    result.append(str(key))
            elif item not in (None, "", [], {}):
                result.append(str(item))
        return [item.strip() for item in result if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [_text(value)] if _text(value) else []


def _story_use(value: Any, *, hooked: bool) -> str:
    if hooked:
        return "hook"
    raw = _text(value).casefold().replace("ё", "е")
    if not raw:
        return "reference"
    hook_tokens = ("hook", "seed", "plot", "story", "trigger", "хук", "крюч", "сюжет", "всплы", "прояв")
    reference_tokens = ("reference", "ref", "canon", "context", "background", "info", "справ", "контекст", "фон", "учит", "хран", "канон")
    if any(token in raw for token in hook_tokens):
        return "hook"
    if any(token in raw for token in reference_tokens):
        return "reference"
    return "reference"


def _normalise_hooks(raw_hooks: Any, fact_ids: set[str]) -> tuple[list[Any], set[str]]:
    if not isinstance(raw_hooks, list):
        return raw_hooks, set()
    result: list[Any] = []
    hooked: set[str] = set()
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_hooks):
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        row = deepcopy(raw)
        refs = row.get("fact_ids")
        if refs is None:
            refs = row.get("source_fact_ids")
        if refs is None:
            refs = row.get("facts")
        if refs is None:
            refs = row.get("fact_id")
        refs = _as_list(refs)
        row["fact_ids"] = refs
        for alias in ("source_fact_ids", "facts", "fact_id"):
            row.pop(alias, None)

        hook_id = _text(row.get("hook_id") or row.get("id") or row.get("key") or row.get("slug"))
        if not hook_id:
            basis = refs[0] if refs else f"{index + 1}"
            hook_id = f"hook_{_slug(basis, str(index + 1))}"
        base_id = hook_id
        suffix = 2
        while hook_id in used_ids:
            hook_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(hook_id)
        row["hook_id"] = hook_id
        for alias in ("id", "key", "slug"):
            row.pop(alias, None)
        if refs:
            hooked.update(ref for ref in refs if ref in fact_ids)
        result.append(row)
    return result, hooked


def _normalise_foundation_coverage(template: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(template)
    foundation = result.get("foundation")
    if not isinstance(foundation, dict):
        return result
    foundation = deepcopy(foundation)

    facts = foundation.get("facts")
    if isinstance(facts, dict):
        expanded = []
        for key, value in facts.items():
            if isinstance(value, dict):
                row = deepcopy(value)
                row.setdefault("fact_id", str(key))
            else:
                row = {"fact_id": str(key), "text": value}
            expanded.append(row)
        facts = expanded
    if not isinstance(facts, list):
        result["foundation"] = foundation
        return result

    normalized_facts: list[Any] = []
    fact_ids: set[str] = set()
    for index, raw in enumerate(facts):
        if not isinstance(raw, dict):
            normalized_facts.append(raw)
            continue
        row = deepcopy(raw)
        fact_id = _text(row.get("fact_id") or row.get("id") or row.get("key"))
        if not fact_id:
            fact_id = f"fact_{index + 1}"
        row["fact_id"] = fact_id
        for alias in ("id", "key"):
            row.pop(alias, None)
        fact_ids.add(fact_id)
        normalized_facts.append(row)

    hooks, hooked_facts = _normalise_hooks(foundation.get("hooks"), fact_ids)
    if isinstance(hooks, list):
        foundation["hooks"] = hooks

    for index, raw in enumerate(normalized_facts):
        if not isinstance(raw, dict):
            continue
        row = raw
        fact_id = _text(row.get("fact_id"))

        stored = row.get("stored_in")
        if stored in (None, "", [], {}):
            for alias in ("stored_at", "storage", "paths", "path", "sections", "section", "locations", "location"):
                if row.get(alias) not in (None, "", [], {}):
                    stored = row.get(alias)
                    break
        stored_list = _as_list(stored)
        if not stored_list and fact_id:
            stored_list = [f"foundation.facts.{fact_id}"]
        row["stored_in"] = stored_list
        for alias in ("stored_at", "storage", "paths", "path", "sections", "section", "locations", "location"):
            row.pop(alias, None)

        raw_use = row.get("story_use")
        if raw_use is None:
            raw_use = row.get("usage")
        use = _story_use(raw_use, hooked=fact_id in hooked_facts)
        row["story_use"] = use
        row.pop("usage", None)

        if use == "hook" and fact_id and fact_id not in hooked_facts and isinstance(foundation.get("hooks"), list):
            auto_id = f"hook_{_slug(fact_id, str(index + 1))}"
            existing_ids = {
                _text(item.get("hook_id"))
                for item in foundation["hooks"]
                if isinstance(item, dict)
            }
            base_id = auto_id
            suffix = 2
            while auto_id in existing_ids:
                auto_id = f"{base_id}_{suffix}"
                suffix += 1
            foundation["hooks"].append({
                "hook_id": auto_id,
                "fact_ids": [fact_id],
                "condition": "когда факт естественно становится уместен",
                "status": "latent",
            })
            hooked_facts.add(fact_id)

    foundation["facts"] = normalized_facts
    result["foundation"] = foundation
    return result


def _normalise(template: Dict[str, Any]) -> Dict[str, Any]:
    base = _ORIGINAL_NORMALISE(template)
    return _normalise_foundation_coverage(base)


def install() -> None:
    global _ORIGINAL_NORMALISE
    if _ORIGINAL_NORMALISE is not None:
        return
    _ORIGINAL_NORMALISE = novel_drafts._normalise_foundation_shape
    novel_drafts._normalise_foundation_shape = _normalise
