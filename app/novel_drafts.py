import json
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from . import storage
from .novel_access import prepare_template_read, verify_template
from .operation_receipts import canonical_hash
from .transactional_storage import session_transaction


REQUIRED_SECTIONS = ("novel", "characters", "lore", "starting_state")
ALLOWED_SECTIONS = {
    "novel",
    "characters",
    "lore",
    "rules",
    "hidden_lore",
    "world",
    "starting_state",
    "story_direction",
    "foundation",
}
_CURRENT_FIELDS = (
    "date",
    "game_date",
    "calendar_date",
    "time",
    "game_time",
    "location",
    "place",
    "area",
    "scene",
    "scene_name",
    "situation",
    "present_characters",
)
_CURRENT_ALIASES = {
    "start_date": "date",
    "current_date": "date",
    "start_time": "time",
    "current_time": "time",
    "start_location": "location",
    "current_location": "location",
    "starting_location": "location",
    "scene_title": "scene",
    "current_scene_name": "scene",
    "present": "present_characters",
    "participants": "present_characters",
    "characters_present": "present_characters",
    "present_character_ids": "present_characters",
}


def _drafts_dir() -> Path:
    path = storage.DATA_DIR / "novel_drafts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _draft_path(draft_id: str) -> Path:
    return _drafts_dir() / f"{draft_id}.json"


def _write(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read(draft_id: str) -> Dict[str, Any]:
    path = _draft_path(draft_id)
    if not path.exists():
        raise FileNotFoundError(draft_id)
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_one_json(text: str) -> Any:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    parsed, end = decoder.raw_decode(value)
    if value[end:].strip():
        raise ValueError("EXTRA_DATA")
    return parsed


def _build_template(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = deepcopy(draft["sections"])
    template = {
        "novel_id": draft["novel_id"],
        "title": draft["title"],
        "version": draft.get("version", 1),
        "novel": sections.pop("novel"),
        "characters": sections.pop("characters"),
        "lore": sections.pop("lore"),
    }
    template.update(sections)
    return template


def _character_ref(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("character_id") or value.get("id") or value.get("name") or value.get("full_name")
    if isinstance(value, (str, int, float)):
        return value
    return None


def _resolve_character_ref(cards: list[Dict[str, Any]], value: Any) -> str | None:
    ref = _character_ref(value)
    if ref is None:
        return None
    needle = str(ref).casefold().replace("ё", "е").strip()
    for card in cards:
        cid = storage._card_id(card)
        if cid.casefold().replace("ё", "е") == needle:
            return cid
        for alias in storage._card_names(card):
            if str(alias).casefold().replace("ё", "е").strip() == needle:
                return cid
    return None


def _merge_current_shape(current: Dict[str, Any], candidate: Any) -> None:
    if not isinstance(candidate, dict):
        return
    for key, value in candidate.items():
        target_key = _CURRENT_ALIASES.get(key, key)
        if target_key in _CURRENT_FIELDS and target_key not in current and value not in (None, "", [], {}):
            current[target_key] = deepcopy(value)


def _pillar_id(value: Any, index: int) -> str:
    text = str(value or "").casefold().replace("ё", "е").strip()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:80] or f"pillar_{index + 1}"


def _normalise_foundation_shape(template: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(template)
    foundation = result.get("foundation")
    if not isinstance(foundation, dict):
        return result
    foundation = deepcopy(foundation)
    raw_pillars = foundation.get("story_pillars")
    if isinstance(raw_pillars, dict):
        expanded = []
        for key, value in raw_pillars.items():
            if isinstance(value, dict):
                row = deepcopy(value)
                row.setdefault("pillar_id", str(key))
            else:
                row = {"pillar_id": str(key), "label": value}
            expanded.append(row)
        raw_pillars = expanded
    if not isinstance(raw_pillars, list):
        result["foundation"] = foundation
        return result

    normalized: list[Dict[str, Any]] = []
    aliases: Dict[str, str] = {}
    used: set[str] = set()
    for index, raw in enumerate(raw_pillars):
        if isinstance(raw, str):
            row: Dict[str, Any] = {"label": raw}
        elif isinstance(raw, dict):
            row = deepcopy(raw)
        else:
            normalized.append(raw)
            continue

        label = str(
            row.get("label")
            or row.get("name")
            or row.get("title")
            or row.get("summary")
            or ""
        ).strip()
        explicit_id = row.get("pillar_id") or row.get("id") or row.get("key") or row.get("slug")
        pid = str(explicit_id or _pillar_id(label, index)).strip()
        base_pid = pid
        suffix = 2
        while pid in used:
            pid = f"{base_pid}_{suffix}"
            suffix += 1
        used.add(pid)

        refs = row.get("source_fact_ids")
        if refs is None:
            refs = row.get("fact_ids")
        if refs is None:
            refs = row.get("facts")
        if refs is None:
            refs = []
        elif isinstance(refs, (str, int, float)):
            refs = [str(refs)]

        clean = deepcopy(row)
        clean["pillar_id"] = pid
        clean["label"] = label or pid
        clean["source_fact_ids"] = refs
        for alias_key in ("id", "key", "slug", "title", "summary", "fact_ids", "facts"):
            clean.pop(alias_key, None)
        normalized.append(clean)

        for alias in (explicit_id, label, raw if isinstance(raw, str) else None, pid):
            if alias not in (None, ""):
                aliases[str(alias).casefold().replace("ё", "е").strip()] = pid

    foundation["story_pillars"] = normalized

    hooks = foundation.get("hooks")
    if isinstance(hooks, list) and aliases:
        rewritten_hooks = []
        for raw in hooks:
            if not isinstance(raw, dict):
                rewritten_hooks.append(raw)
                continue
            hook = deepcopy(raw)
            refs = hook.get("pillar_ids")
            if isinstance(refs, (str, int, float)):
                refs = [refs]
            if isinstance(refs, list):
                hook["pillar_ids"] = [
                    aliases.get(str(value).casefold().replace("ё", "е").strip(), str(value))
                    for value in refs
                ]
            rewritten_hooks.append(hook)
        foundation["hooks"] = rewritten_hooks

    result["foundation"] = foundation
    return result


def _foundation_coverage(template: Dict[str, Any], *, required: bool) -> Dict[str, Any]:
    foundation = template.get("foundation")
    if not required and not isinstance(foundation, dict):
        return {"required": False, "ok": True, "fact_count": 0, "hook_count": 0, "pillar_count": 0, "unmapped": []}
    if not isinstance(foundation, dict):
        raise ValueError("FOUNDATION_REQUIRED")

    facts = foundation.get("facts")
    hooks = foundation.get("hooks")
    pillars = foundation.get("story_pillars")
    if not isinstance(facts, list) or not facts:
        raise ValueError("FOUNDATION_FACTS_REQUIRED")
    if not isinstance(hooks, list):
        raise ValueError("FOUNDATION_HOOKS_REQUIRED")
    if not isinstance(pillars, list) or not pillars:
        raise ValueError("FOUNDATION_STORY_PILLARS_REQUIRED")

    fact_ids: set[str] = set()
    fact_rows: Dict[str, Dict[str, Any]] = {}
    for row in facts:
        if not isinstance(row, dict):
            raise ValueError("FOUNDATION_FACT_INVALID")
        fact_id = str(row.get("fact_id") or "").strip()
        text = str(row.get("text") or "").strip()
        if not fact_id or not text or fact_id in fact_ids:
            raise ValueError("FOUNDATION_FACT_INVALID")
        fact_ids.add(fact_id)
        fact_rows[fact_id] = row

    hook_ids: set[str] = set()
    hooked_facts: set[str] = set()
    for row in hooks:
        if not isinstance(row, dict):
            raise ValueError("FOUNDATION_HOOK_INVALID")
        hook_id = str(row.get("hook_id") or "").strip()
        refs = row.get("fact_ids")
        if not hook_id or hook_id in hook_ids or not isinstance(refs, list) or not refs:
            raise ValueError("FOUNDATION_HOOK_INVALID")
        hook_ids.add(hook_id)
        for value in refs:
            ref = str(value)
            if ref not in fact_ids:
                raise ValueError("FOUNDATION_HOOK_UNKNOWN_FACT")
            hooked_facts.add(ref)

    pillar_ids: set[str] = set()
    for row in pillars:
        if not isinstance(row, dict):
            raise ValueError("FOUNDATION_STORY_PILLAR_INVALID")
        pillar_id = str(row.get("pillar_id") or "").strip()
        label = str(row.get("label") or row.get("name") or "").strip()
        if not pillar_id or not label or pillar_id in pillar_ids:
            raise ValueError("FOUNDATION_STORY_PILLAR_INVALID")
        pillar_ids.add(pillar_id)
        refs = row.get("source_fact_ids", [])
        if refs is not None and not isinstance(refs, list):
            raise ValueError("FOUNDATION_STORY_PILLAR_INVALID")
        for value in refs or []:
            if str(value) not in fact_ids:
                raise ValueError("FOUNDATION_STORY_PILLAR_UNKNOWN_FACT")

    unmapped: list[str] = []
    for fact_id, row in fact_rows.items():
        stored_in = row.get("stored_in")
        use = str(row.get("story_use") or row.get("usage") or "").casefold().strip()
        if not isinstance(stored_in, list) or not [value for value in stored_in if str(value).strip()]:
            unmapped.append(fact_id)
            continue
        if use not in {"reference", "hook"}:
            unmapped.append(fact_id)
            continue
        if use == "hook" and fact_id not in hooked_facts:
            unmapped.append(fact_id)

    if unmapped:
        raise ValueError("FOUNDATION_COVERAGE_INCOMPLETE:" + ",".join(unmapped[:20]))
    return {
        "required": required,
        "ok": True,
        "fact_count": len(fact_ids),
        "hook_count": len(hook_ids),
        "pillar_count": len(pillar_ids),
        "unmapped": [],
    }


def _normalise_starting_state_for_session(template: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(template)
    cards = storage._normalise_cards(result.get("characters", []))
    raw = result.get("starting_state")
    state = deepcopy(raw) if isinstance(raw, dict) else {}

    current = state.get("current")
    if not isinstance(current, dict):
        current = {}
    else:
        current = deepcopy(current)
    _merge_current_shape(current, current)
    for container_key in ("current_scene", "scene_state", "start", "initial_scene"):
        _merge_current_shape(current, state.get(container_key))
    _merge_current_shape(current, state)

    present_supplied = "present_characters" in current
    present = current.get("present_characters", [])
    if isinstance(present, dict):
        present = list(present.keys())
    elif present is None:
        present = []
    elif not isinstance(present, list):
        present = [present]
    canonical_present = []
    for value in present:
        resolved = _resolve_character_ref(cards, value)
        if resolved:
            canonical_present.append(resolved)
    if present_supplied:
        current["present_characters"] = list(dict.fromkeys(canonical_present))

    pov_raw = state.get("pov") or state.get("pov_character_id") or state.get("pov_character") or state.get("protagonist")
    if isinstance(pov_raw, dict):
        pov = deepcopy(pov_raw)
        resolved = _resolve_character_ref(cards, pov.get("character_id") or pov.get("id") or pov.get("name"))
        if resolved:
            pov["character_id"] = resolved
    else:
        resolved = _resolve_character_ref(cards, pov_raw)
        pov = {"character_id": resolved} if resolved else {}
    if not pov.get("character_id"):
        inferred = storage._find_pov_id(result, cards)
        if inferred:
            pov["character_id"] = inferred
    state["pov"] = pov

    pov_id = str(pov.get("character_id") or "")
    if present_supplied and canonical_present and pov_id and pov_id not in canonical_present:
        canonical_present.insert(0, pov_id)
        current["present_characters"] = list(dict.fromkeys(canonical_present))
    state["current"] = current

    if not isinstance(state.get("characters"), dict):
        state["characters"] = {}
    if not isinstance(state.get("relationships"), dict):
        state["relationships"] = {}
    if not isinstance(state.get("threads"), (dict, list)):
        state["threads"] = {}
    if not isinstance(state.get("world"), dict):
        state["world"] = {}

    foundation = result.get("foundation") if isinstance(result.get("foundation"), dict) else {}
    if foundation:
        world = deepcopy(state["world"])
        hook_state = world.get("foundation_hook_state") if isinstance(world.get("foundation_hook_state"), dict) else {}
        hook_state = deepcopy(hook_state)
        for hook in foundation.get("hooks", []) if isinstance(foundation.get("hooks"), list) else []:
            if isinstance(hook, dict) and hook.get("hook_id"):
                hook_state.setdefault(str(hook["hook_id"]), {"status": str(hook.get("status") or "latent"), "touch_count": 0, "last_touched_turn": 0})
        world["foundation_hook_state"] = hook_state

        story_pillars = world.get("story_pillars") if isinstance(world.get("story_pillars"), dict) else {}
        story_pillars = deepcopy(story_pillars)
        for pillar in foundation.get("story_pillars", []) if isinstance(foundation.get("story_pillars"), list) else []:
            if not isinstance(pillar, dict) or not pillar.get("pillar_id"):
                continue
            pid = str(pillar["pillar_id"])
            story_pillars.setdefault(pid, {
                "label": pillar.get("label") or pillar.get("name") or pid,
                "source_fact_ids": deepcopy(pillar.get("source_fact_ids", [])),
                "last_touched_turn": 0,
                "status": "active",
            })
        world["story_pillars"] = story_pillars
        social = world.get("social") if isinstance(world.get("social"), dict) else {}
        social.setdefault("signals", {})
        world["social"] = social
        state["world"] = world

    result["starting_state"] = state
    return result


def _validate_starting_state(template: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _normalise_starting_state_for_session(template)
    state = normalized.get("starting_state") if isinstance(normalized.get("starting_state"), dict) else {}
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    cards = storage._normalise_cards(normalized.get("characters", []))
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or "")

    pointer = (
        current.get("date") or current.get("game_date") or current.get("calendar_date"),
        current.get("time") or current.get("game_time"),
        current.get("location") or current.get("place") or current.get("area"),
    )
    present = current.get("present_characters") if isinstance(current.get("present_characters"), list) else []
    present = [str(value) for value in present if value]
    valid_ids = {storage._card_id(card) for card in cards}

    if not any(value not in (None, "", [], {}) for value in pointer):
        raise ValueError("STARTING_STATE_SCENE_POINTER_REQUIRED")
    if not pov_id or pov_id not in valid_ids:
        raise ValueError("STARTING_STATE_POV_REQUIRED")
    if not present:
        raise ValueError("STARTING_STATE_PRESENT_CHARACTERS_REQUIRED")
    if pov_id not in present:
        raise ValueError("STARTING_STATE_POV_MUST_BE_PRESENT")
    return normalized


def _validate_template(template: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    normalized = _normalise_foundation_shape(template)
    normalized = _validate_starting_state(normalized)
    version = int(normalized.get("version", 1) or 1)
    coverage = _foundation_coverage(normalized, required=version >= 2)
    return normalized, coverage


def create_draft(novel_id: str, title: str, version: int = 1) -> Dict[str, Any]:
    draft_id = uuid.uuid4().hex
    draft = {
        "draft_id": draft_id,
        "novel_id": novel_id,
        "title": title,
        "version": version,
        "revision": 0,
        "sections": {},
        "finalized": False,
        "published_to_library": False,
    }
    _write(_draft_path(draft_id), draft)
    return draft_status(draft_id)


def save_section(draft_id: str, section_name: str, section_json: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    was_finalized = bool(draft.get("finalized"))
    section_name = section_name.strip()
    if section_name not in ALLOWED_SECTIONS:
        raise KeyError(section_name)
    parsed = _parse_one_json(section_json)
    if section_name == "characters" and not isinstance(parsed, list):
        raise TypeError("characters must be a JSON array")
    if section_name == "starting_state" and not isinstance(parsed, dict):
        raise TypeError("starting_state must be a JSON object")
    if section_name == "foundation" and not isinstance(parsed, dict):
        raise TypeError("foundation must be a JSON object")
    draft["sections"][section_name] = parsed
    draft["revision"] = int(draft.get("revision", 0) or 0) + 1
    draft["finalized"] = False
    draft.pop("finalized_template", None)
    if int(draft.get("version", 1) or 1) >= 3:
        draft.pop("launch_state", None)
        draft.pop("launch_state_hash", None)
        draft.pop("launch_hint", None)
        draft.pop("session_creation_receipt", None)
    _write(_draft_path(draft_id), draft)
    result = dict(draft_status(draft_id))
    result["reopened_from_finalized"] = was_finalized
    return result


def draft_status(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    sections = draft.get("sections", {})
    characters = sections.get("characters", [])
    required = list(REQUIRED_SECTIONS)
    if int(draft.get("version", 1) or 1) >= 2:
        required.append("foundation")
    missing = [name for name in required if name not in sections]
    blocker = None
    coverage = None

    base_ready = not missing and isinstance(characters, list) and len(characters) > 0
    if base_ready:
        try:
            _normalized, coverage = _validate_template(_build_template(draft))
        except ValueError as exc:
            blocker = str(exc)

    return {
        "draft_id": draft_id,
        "novel_id": draft["novel_id"],
        "title": draft["title"],
        "version": draft.get("version", 1),
        "revision": int(draft.get("revision", 0) or 0),
        "saved_sections": sorted(sections.keys()),
        "missing_required_sections": missing,
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "ready_to_finalize": base_ready and blocker is None,
        "finalize_blocker": blocker,
        "foundation_coverage": coverage,
        "finalized": bool(draft.get("finalized")),
        "published_to_library": bool(draft.get("published_to_library")),
    }


def finalize_draft(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    status = draft_status(draft_id)
    if not status["ready_to_finalize"]:
        raise ValueError(status.get("finalize_blocker") or "DRAFT_INCOMPLETE")
    template, coverage = _validate_template(_build_template(draft))
    verification = verify_template(template)
    if not verification["ok"]:
        raise RuntimeError("FINAL_VERIFICATION_FAILED")
    draft["finalized"] = True
    draft["finalized_revision"] = int(draft.get("revision", 0) or 0)
    draft["finalized_template"] = template
    _write(_draft_path(draft_id), draft)
    return {
        "ok": True,
        "draft_id": draft_id,
        "verification": verification,
        "foundation_coverage": coverage,
        "saved_to_library": False,
        "instruction": "Draft is verified but NOT added to the library. Read it with prepareDraftRead, then createSessionFromDraft. If setup omissions are found before the first gameplay turn, save corrected sections to this same draft_id to reopen it, re-read, re-verify and re-finalize before creating a replacement session.",
    }


def _finalized_template(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    template = draft.get("finalized_template")
    if not draft.get("finalized") or not isinstance(template, dict):
        raise RuntimeError("DRAFT_NOT_FINALIZED")
    return template


def prepare_draft_read(draft_id: str) -> Dict[str, Any]:
    return prepare_template_read(_finalized_template(draft_id), "draft", draft_id)


def _existing_session_for_draft(
    draft_id: str,
    revision: int,
    template: Dict[str, Any],
) -> Dict[str, Any] | None:
    if not storage.SESSIONS_DIR.exists():
        return None
    expected_hash = canonical_hash(template)
    for root in storage.SESSIONS_DIR.iterdir():
        if not root.is_dir():
            continue
        meta = storage._read_json(root / "meta.json", {})
        if not isinstance(meta, dict) or str(meta.get("source_draft_id") or "") != draft_id:
            continue
        saved_revision = meta.get("source_draft_revision")
        if saved_revision is not None:
            try:
                if int(saved_revision) != int(revision):
                    continue
            except (TypeError, ValueError):
                continue
        elif int(meta.get("turn_number", 0) or 0) != 0:
            continue
        source = storage._read_json(root / "source.json", {})
        if isinstance(source, dict) and canonical_hash(source) == expected_hash:
            result = deepcopy(meta)
            result["already_created"] = True
            result["idempotent_replay"] = True
            return result
    return None


def create_session_from_draft(draft_id: str) -> Dict[str, Any]:
    with session_transaction(_drafts_dir()):
        draft = _read(draft_id)
        template = draft.get("finalized_template")
        if not draft.get("finalized") or not isinstance(template, dict):
            raise RuntimeError("DRAFT_NOT_FINALIZED")
        template, coverage = _validate_template(template)
        revision = int(draft.get("finalized_revision", draft.get("revision", 0)) or 0)
        template_hash = canonical_hash(template)

        receipt = draft.get("session_creation_receipt")
        if isinstance(receipt, dict):
            session_id = str(receipt.get("session_id") or "")
            if (
                int(receipt.get("draft_revision", -1) or -1) == revision
                and str(receipt.get("template_hash") or "") == template_hash
                and session_id
                and (storage.SESSIONS_DIR / session_id).exists()
            ):
                existing = storage._read_json(storage.SESSIONS_DIR / session_id / "meta.json", {})
                if isinstance(existing, dict) and existing.get("session_id") == session_id:
                    result = deepcopy(existing)
                    result["already_created"] = True
                    result["idempotent_replay"] = True
                    return result

        existing = _existing_session_for_draft(draft_id, revision, template)
        if existing is not None:
            draft["session_creation_receipt"] = {
                "draft_revision": revision,
                "template_hash": template_hash,
                "session_id": existing["session_id"],
            }
            _write(_draft_path(draft_id), draft)
            return existing

        deterministic_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"roman-ai:draft:{draft_id}:revision:{revision}:template:{template_hash}",
        ).hex
        meta = storage.create_session(
            template,
            session_id=deterministic_id,
            meta_patch={
                "source_type": "session_draft",
                "source_draft_id": draft_id,
                "source_draft_revision": revision,
                "source_draft_hash": template_hash,
                "foundation_coverage": coverage,
            },
        )
        draft["session_creation_receipt"] = {
            "draft_revision": revision,
            "template_hash": template_hash,
            "session_id": meta["session_id"],
        }
        _write(_draft_path(draft_id), draft)
        result = deepcopy(meta)
        result["already_created"] = False
        result["idempotent_replay"] = False
        return result


def publish_draft_to_library(draft_id: str) -> Dict[str, Any]:
    draft = _read(draft_id)
    template, _coverage = _validate_template(_finalized_template(draft_id))
    storage.save_novel(template)
    draft["published_to_library"] = True
    _write(_draft_path(draft_id), draft)
    return {
        "ok": True,
        "novel_id": template["novel_id"],
        "title": template["title"],
        "published_to_library": True,
    }