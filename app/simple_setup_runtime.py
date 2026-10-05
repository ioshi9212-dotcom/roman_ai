from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import draft_intake_runtime, novel_access, novel_drafts, setup_draft_v3_runtime
from .operation_receipts import canonical_hash
from .profile_templates import (
    normalize_canon_notes,
    normalize_character_profiles,
    normalize_hidden_lore,
    normalize_location_profiles,
    normalize_novel_profile,
    profile_manifest,
)
from .transactional_storage import session_transaction


SIMPLE_PROFILE_DRAFT_VERSION = 5

_ORIGINAL_SAVE_SECTION = None
_ORIGINAL_DRAFT_STATUS = None
_ORIGINAL_FINALIZE = None
_ORIGINAL_VALIDATE_TEMPLATE = None
_ORIGINAL_APPEND_INTAKE = None
_ORIGINAL_UPDATE_MAPPING = None
_ORIGINAL_COVERAGE = None
_ORIGINAL_CONFIRM_RECONCILIATION = None
_ORIGINAL_SETUP_STATUS = None


def _is_simple_draft(draft: Dict[str, Any]) -> bool:
    try:
        return int(draft.get("version", 1) or 1) >= SIMPLE_PROFILE_DRAFT_VERSION
    except (TypeError, ValueError):
        return False


def _simple_intake_coverage(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
    intake = sections.get("intake")
    if not isinstance(intake, dict):
        return {
            "required": True,
            "ok": False,
            "block_count": 0,
            "reviewed_blocks": 0,
            "unreviewed_blocks": [],
            "simple_profile_mode": True,
        }
    blocks = intake.get("blocks") if isinstance(intake.get("blocks"), list) else []
    valid_blocks = [
        row for row in blocks
        if isinstance(row, dict)
        and str(row.get("block_id") or "").strip()
        and str(row.get("stage") or "").strip()
        and str(row.get("raw_text") or "").strip()
    ]
    unreviewed = [
        str(row.get("block_id") or "")
        for row in valid_blocks
        if row.get("reviewed_against_raw") is not True
    ]
    block_count = len(valid_blocks)
    return {
        "required": True,
        "ok": block_count > 0 and not unreviewed,
        "block_count": block_count,
        "reviewed_blocks": block_count - len(unreviewed),
        "unreviewed_blocks": unreviewed,
        "source_unit_count": 0,
        "covered_source_unit_count": 0,
        "uncovered_source_units": [],
        "unknown_source_unit_ids": [],
        "unknown_fact_ids": [],
        "simple_profile_mode": True,
        "instruction": (
            "RAW intake stays verbatim. For profile draft v5 there is no fact-id/source-unit mapping. "
            "A block is reviewed when its information has been placed into the fixed novel/character/hidden-lore profiles."
        ),
    }


def _coverage(draft: Dict[str, Any]) -> Dict[str, Any]:
    if _is_simple_draft(draft):
        return _simple_intake_coverage(draft)
    return _ORIGINAL_COVERAGE(draft)


def _append_intake_chunk(
    draft_id: str,
    *,
    block_id: str,
    stage: str,
    chunk_index: int,
    raw_text: str,
    is_last: bool,
) -> Dict[str, Any]:
    result = dict(_ORIGINAL_APPEND_INTAKE(
        draft_id,
        block_id=block_id,
        stage=stage,
        chunk_index=chunk_index,
        raw_text=raw_text,
        is_last=is_last,
    ))
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft):
        return result
    result.pop("source_units", None)
    result.pop("source_unit_count", None)
    result["simple_profile_mode"] = True
    result["instruction"] = (
        "RAW сохранён дословно. Не создавай fact_id/source_unit_id. После команды «подтверждаю» "
        "разложи весь RAW по фиксированным профилям и отметь этот блок reviewed_against_raw=true."
    )
    return result


def _update_intake_mapping(
    draft_id: str,
    block_id: str,
    *,
    fact_ids: List[str],
    reviewed_against_raw: bool,
    contains_no_facts: bool,
    replace: bool = False,
    expected_revision: int | None = None,
) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft):
        return _ORIGINAL_UPDATE_MAPPING(
            draft_id,
            block_id,
            fact_ids=fact_ids,
            reviewed_against_raw=reviewed_against_raw,
            contains_no_facts=contains_no_facts,
            replace=replace,
            expected_revision=expected_revision,
        )
    if fact_ids:
        raise ValueError("SIMPLE_PROFILE_MAPPING_HAS_NO_FACT_IDS")
    if not reviewed_against_raw:
        raise ValueError("SIMPLE_PROFILE_REVIEW_REQUIRED")

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        revision_before = int(draft.get("revision", 0) or 0)
        if expected_revision is not None and int(expected_revision) != revision_before:
            raise ValueError("INTAKE_MAPPING_REVISION_MISMATCH")

        sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
        intake = sections.get("intake")
        if not isinstance(intake, dict):
            raise ValueError("INTAKE_BLOCK_NOT_FOUND")
        normalized = draft_intake_runtime._normalise_intake(intake)
        target = next(
            (row for row in normalized.get("blocks", []) if str(row.get("block_id") or "") == str(block_id)),
            None,
        )
        if target is None:
            raise ValueError("INTAKE_BLOCK_NOT_FOUND")

        changed = target.get("reviewed_against_raw") is not True or target.get("fact_ids") != []
        target["fact_ids"] = []
        target["reviewed_against_raw"] = True
        target["contains_no_facts"] = False

        if changed:
            draft.setdefault("sections", {})["intake"] = {
                "version": int(normalized.get("version", draft_intake_runtime._VERSION) or draft_intake_runtime._VERSION),
                "blocks": normalized["blocks"],
            }
            draft["revision"] = revision_before + 1
            draft["finalized"] = False
            draft.pop("finalized_template", None)
            draft.pop("launch_state", None)
            draft.pop("launch_state_hash", None)
            draft.pop("launch_hint", None)
            draft.pop("session_creation_receipt", None)
            draft.pop("reconciliation_receipt", None)
            novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    result = dict(_draft_status(draft_id))
    result.update({
        "block_id": block_id,
        "mapping_changed": changed,
        "simple_profile_mode": True,
        "draft_revision": int(novel_drafts._read(draft_id).get("revision", 0) or 0),
    })
    return result


def _normalise_simple_section(draft: Dict[str, Any], section_name: str, value: Any) -> Any:
    name = str(section_name or "").strip()
    if name == "novel":
        return normalize_novel_profile(value, title=str(draft.get("title") or ""))
    if name == "characters":
        return normalize_character_profiles(value)
    if name == "hidden_lore":
        return normalize_hidden_lore(value)
    if name == "locations":
        return normalize_location_profiles(value)
    if name == "canon_notes":
        return normalize_canon_notes(value)
    if name == "knowledge":
        # Optional explicit pre-story knowledge. Runtime knowledge learned later is appended during play.
        return deepcopy(value) if isinstance(value, dict) else {}
    if name == "lore":
        if isinstance(value, dict):
            return deepcopy(value)
        if value in (None, ""):
            return {}
        return {"text": str(value)}
    return deepcopy(value)


def _save_section(
    draft_id: str,
    section_name: str,
    section_json: str,
    expected_revision: int | None = None,
) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft) or section_name.strip() == "intake":
        return _ORIGINAL_SAVE_SECTION(
            draft_id,
            section_name,
            section_json,
            expected_revision=expected_revision,
        )

    raw = novel_drafts._parse_one_json(section_json)
    normalized = _normalise_simple_section(draft, section_name, raw)

    # Idempotent v5 writes must not create a new draft revision. During
    # reconciliation GPT may resend an already-correct normalized section; that
    # is not a content change and must not invalidate the completed full read.
    sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
    existing = sections.get(section_name.strip())
    current_revision = int(draft.get("revision", 0) or 0)
    if expected_revision is not None and int(expected_revision) != current_revision:
        raise ValueError("DRAFT_SECTION_REVISION_MISMATCH")
    if existing == normalized:
        result = dict(_draft_status(draft_id))
        result.update({
            "section_name": section_name.strip(),
            "section_changed": False,
            "idempotent_replay": True,
            "draft_revision": current_revision,
        })
        return result

    result = dict(_ORIGINAL_SAVE_SECTION(
        draft_id,
        section_name,
        json.dumps(normalized, ensure_ascii=False),
        expected_revision=expected_revision,
    ))
    result["section_name"] = section_name.strip()
    result["section_changed"] = True
    result["idempotent_replay"] = False
    return result


def _ensure_core_cast(novel: Dict[str, Any], characters: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = deepcopy(novel)
    existing = result.get("core_cast")
    if isinstance(existing, list) and existing:
        return result

    pov_ref = str(result.get("pov_character") or "").casefold().strip()
    rows: List[Dict[str, Any]] = []
    for card in characters:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("character_id") or "")
        name = " ".join(
            part for part in (str(card.get("name") or "").strip(), str(card.get("surname") or "").strip())
            if part
        ) or cid
        role = str(card.get("role") or "").casefold()
        aliases = {cid.casefold(), str(card.get("name") or "").casefold(), name.casefold()}
        is_core = bool(card.get("is_pov")) or pov_ref in aliases or role in {
            "pov", "main", "major", "core", "главный", "главная", "основной", "основная",
        }
        if not is_core:
            continue
        rows.append({
            "character_id": cid,
            "name": name,
            "story_function": str(card.get("story_function") or card.get("role") or "участник основной линии"),
        })

    if not rows and characters:
        card = characters[0]
        rows.append({
            "character_id": str(card.get("character_id") or ""),
            "name": " ".join(
                part for part in (str(card.get("name") or "").strip(), str(card.get("surname") or "").strip())
                if part
            ),
            "story_function": str(card.get("story_function") or card.get("role") or "участник основной линии"),
        })
    result["core_cast"] = rows
    return result


def _content_template(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = deepcopy(draft.get("sections", {})) if isinstance(draft.get("sections"), dict) else {}
    intake = sections.pop("intake", None)
    sections.pop("starting_state", None)

    characters = normalize_character_profiles(sections.pop("characters", []))
    novel = normalize_novel_profile(sections.pop("novel", {}), title=str(draft.get("title") or ""))
    novel = _ensure_core_cast(novel, characters)

    lore = sections.pop("lore", {})
    if not isinstance(lore, dict):
        lore = {"text": str(lore)} if lore not in (None, "") else {}

    hidden_lore = normalize_hidden_lore(sections.pop("hidden_lore", {}))
    locations = normalize_location_profiles(sections.pop("locations", []))
    canon_notes = normalize_canon_notes(sections.pop("canon_notes", []))
    starting_knowledge = sections.pop("knowledge", {})
    if not isinstance(starting_knowledge, dict):
        starting_knowledge = {}

    template: Dict[str, Any] = {
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": int(draft.get("version", SIMPLE_PROFILE_DRAFT_VERSION) or SIMPLE_PROFILE_DRAFT_VERSION),
        "novel": novel,
        "characters": characters,
        "lore": lore,
        "hidden_lore": hidden_lore,
        "locations": locations,
        "canon_notes": canon_notes,
        "knowledge": deepcopy(starting_knowledge),
        "profile_schema": profile_manifest(),
    }
    template.update(sections)
    if isinstance(intake, dict):
        # Keep the author's exact text available after finalization. This is an
        # audit source, not another set of facts or character knowledge.
        template["source_intake"] = [
            {key: deepcopy(block[key]) for key in ("block_id", "stage", "raw_text") if key in block}
            for block in intake.get("blocks", [])
            if isinstance(block, dict) and isinstance(block.get("raw_text"), str)
        ]
    return template


def _resolve_pov(template: Dict[str, Any]) -> str | None:
    characters = template.get("characters") if isinstance(template.get("characters"), list) else []
    novel = template.get("novel") if isinstance(template.get("novel"), dict) else {}
    ref = str(novel.get("pov_character") or "").casefold().strip()
    for card in characters:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("character_id") or "")
        name = str(card.get("name") or "")
        full = " ".join(part for part in (name, str(card.get("surname") or "")) if part)
        if card.get("is_pov") is True or ref in {cid.casefold(), name.casefold(), full.casefold()}:
            return cid
    return None


def _location_identity_key(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е").strip()
    return " ".join(text.replace("-", " ").replace("_", " ").split())


def _validate_location_identity_uniqueness(locations: List[Dict[str, Any]]) -> None:
    seen_locations: set[str] = set()
    for profile in locations:
        if not isinstance(profile, dict):
            continue
        location_id = str(profile.get("location_id") or "").strip()
        key = _location_identity_key(location_id)
        if not key:
            continue
        if key in seen_locations:
            raise ValueError("DRAFT_LOCATION_ID_DUPLICATE")
        seen_locations.add(key)

        seen_zones: set[str] = set()
        for zone in profile.get("zones", []) if isinstance(profile.get("zones"), list) else []:
            if not isinstance(zone, dict):
                continue
            zone_id = str(zone.get("zone_id") or "").strip()
            zone_key = _location_identity_key(zone_id)
            if not zone_key:
                continue
            if zone_key in seen_zones:
                raise ValueError("DRAFT_LOCATION_ZONE_ID_DUPLICATE")
            seen_zones.add(zone_key)


def _canonicalize_location_character_links(
    locations: List[Dict[str, Any]],
    characters: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    aliases: Dict[str, set[str]] = {}
    for card in characters:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("character_id") or "").strip()
        if not cid:
            continue
        values = [
            cid,
            card.get("name"),
            " ".join(
                part for part in (
                    str(card.get("name") or "").strip(),
                    str(card.get("surname") or "").strip(),
                )
                if part
            ),
            *(card.get("aliases") if isinstance(card.get("aliases"), list) else []),
        ]
        for value in values:
            key = str(value or "").casefold().replace("ё", "е").strip()
            if key:
                aliases.setdefault(key, set()).add(cid)

    result = deepcopy(locations)
    for profile in result:
        linked = profile.get("linked_characters") if isinstance(profile.get("linked_characters"), list) else []
        normalized = []
        for row in linked:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("character_id") or "").strip()
            matches = aliases.get(raw.casefold().replace("ё", "е").strip(), set())
            if not matches:
                raise ValueError("DRAFT_LOCATION_CHARACTER_UNKNOWN")
            if len(matches) != 1:
                raise ValueError("DRAFT_LOCATION_CHARACTER_AMBIGUOUS")
            resolved = next(iter(matches))
            clean = deepcopy(row)
            clean["character_id"] = resolved
            normalized.append(clean)
        profile["linked_characters"] = normalized
    return result


def _relationship_identity(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").replace("-", " ").replace("_", " ").split())


def _character_alias_index(characters: List[Dict[str, Any]]) -> Dict[str, set[str]]:
    index: Dict[str, set[str]] = {}
    for card in characters:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("character_id") or "").strip()
        if not cid:
            continue
        full = " ".join(
            part for part in (
                str(card.get("name") or "").strip(),
                str(card.get("surname") or "").strip(),
            )
            if part
        )
        values = [
            cid,
            card.get("name"),
            full,
            *(card.get("aliases") if isinstance(card.get("aliases"), list) else []),
        ]
        for value in values:
            key = _relationship_identity(value)
            if key:
                index.setdefault(key, set()).add(cid)
    return index


def _relationship_dimensions(raw: Any) -> List[Dict[str, Any]]:
    if raw in (None, "", [], {}):
        return []
    if isinstance(raw, dict):
        raw = [{"label": key, "value": value} for key, value in raw.items()]
    if not isinstance(raw, list):
        raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_INVALID")

    result: List[Dict[str, Any]] = []
    seen: Dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_INVALID")
        label = str(item.get("label") or item.get("key") or "").strip()
        value = item.get("value")
        if not label or not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_INVALID")
        numeric = float(value)
        if numeric < 0 or numeric > 100:
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_INVALID")
        if numeric == 0:
            continue
        key = _relationship_identity(label)
        if key in seen and seen[key] != numeric:
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSION_CONFLICT")
        if key in seen:
            continue
        seen[key] = numeric
        result.append({
            "label": label,
            "value": int(numeric) if numeric.is_integer() else numeric,
        })
    if len(result) > 10:
        raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_LIMIT")
    return result


def _merge_relationship_text(current: Any, incoming: Any) -> str | None:
    parts: List[str] = []
    for value in (current, incoming):
        text = " ".join(str(value or "").split())
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts) if parts else None


def _merge_relationship_list(current: Any, incoming: Any) -> List[str]:
    result: List[str] = []
    for source in (current, incoming):
        rows = source if isinstance(source, list) else []
        for value in rows:
            text = " ".join(str(value or "").split())
            if text and text not in result:
                result.append(text)
    return result


def _canonicalize_character_relationships(
    characters: List[Dict[str, Any]],
    pov_id: str,
) -> List[Dict[str, Any]]:
    result = deepcopy(characters)
    aliases = _character_alias_index(result)

    for card in result:
        if not isinstance(card, dict):
            continue
        owner_id = str(card.get("character_id") or "").strip()
        raw = card.get("relationships")
        if raw in (None, "", [], {}):
            card["relationships"] = None
            continue

        if isinstance(raw, str):
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_STRUCTURE_REQUIRED")
        if isinstance(raw, dict):
            if any(key in raw for key in ("target_character_id", "target_id", "target", "with", "character_id")):
                source_rows = [raw]
            else:
                source_rows = []
                for key, value in raw.items():
                    if isinstance(value, dict):
                        row = deepcopy(value)
                        row.setdefault("target_character_id", key)
                    else:
                        raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_STRUCTURE_REQUIRED")
                    source_rows.append(row)
        elif isinstance(raw, list):
            source_rows = raw
        else:
            raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_STRUCTURE_REQUIRED")

        grouped: Dict[str, Dict[str, Any]] = {}
        for raw_row in source_rows:
            if not isinstance(raw_row, dict):
                raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_STRUCTURE_REQUIRED")
            target_raw = (
                raw_row.get("target_character_id")
                or raw_row.get("target_id")
                or raw_row.get("target")
                or raw_row.get("with")
                or raw_row.get("character_id")
            )
            key = _relationship_identity(target_raw)
            matches = aliases.get(key, set()) if key else set()
            if not matches:
                raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_TARGET_UNKNOWN")
            if len(matches) != 1:
                raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_TARGET_AMBIGUOUS")
            target_id = next(iter(matches))
            if target_id == owner_id:
                raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_SELF_TARGET")

            dims = _relationship_dimensions(raw_row.get("dimensions"))
            if owner_id != pov_id and target_id == pov_id and not dims:
                raise ValueError("DRAFT_NPC_POV_RELATIONSHIP_DIMENSIONS_REQUIRED")
            if owner_id != pov_id and target_id != pov_id and dims:
                raise ValueError("DRAFT_NPC_NPC_RELATIONSHIP_MUST_BE_QUALITATIVE")

            row = grouped.setdefault(target_id, {
                "target_character_id": target_id,
                "dimensions": [],
            })

            existing_dims = {
                _relationship_identity(item.get("label")): item
                for item in row.get("dimensions", [])
                if isinstance(item, dict)
            }
            for dim in dims:
                dim_key = _relationship_identity(dim.get("label"))
                existing = existing_dims.get(dim_key)
                if existing and existing.get("value") != dim.get("value"):
                    raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSION_CONFLICT")
                if not existing:
                    row["dimensions"].append(deepcopy(dim))
                    existing_dims[dim_key] = row["dimensions"][-1]
            if len(row["dimensions"]) > 10:
                raise ValueError("DRAFT_CHARACTER_RELATIONSHIP_DIMENSIONS_LIMIT")

            for field in ("relationship_type", "relationship_context", "current_dynamic", "behavioral_pattern"):
                merged = _merge_relationship_text(row.get(field), raw_row.get(field))
                if merged:
                    row[field] = merged

            for field in (
                "beliefs_about_target",
                "unresolved_between_them",
                "dynamic_constraints",
                "interaction_hooks",
            ):
                merged_list = _merge_relationship_list(row.get(field), raw_row.get(field))
                if merged_list:
                    row[field] = merged_list

            if raw_row.get("status") not in (None, ""):
                row["status"] = str(raw_row.get("status")).strip()

        card["relationships"] = list(grouped.values()) or None

    return result


def _validate_simple_content(template: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    normalized = deepcopy(template)
    normalized["characters"] = normalize_character_profiles(normalized.get("characters", []))
    normalized["novel"] = _ensure_core_cast(
        normalize_novel_profile(normalized.get("novel", {}), title=str(normalized.get("title") or "")),
        normalized["characters"],
    )
    pov_id = _resolve_pov(normalized)
    if not pov_id:
        raise ValueError("DRAFT_POV_REQUIRED")
    normalized["characters"] = _canonicalize_character_relationships(
        normalized["characters"],
        pov_id,
    )
    normalized["hidden_lore"] = normalize_hidden_lore(normalized.get("hidden_lore", {}))
    normalized_locations = normalize_location_profiles(normalized.get("locations", []))
    _validate_location_identity_uniqueness(normalized_locations)
    normalized["locations"] = _canonicalize_location_character_links(
        normalized_locations,
        normalized["characters"],
    )
    normalized["canon_notes"] = normalize_canon_notes(normalized.get("canon_notes", []))
    if not isinstance(normalized.get("knowledge"), dict):
        normalized["knowledge"] = {}

    if not normalized["characters"]:
        raise ValueError("DRAFT_CHARACTERS_REQUIRED")

    verification = novel_drafts.verify_template(normalized)
    if not verification.get("ok"):
        raise ValueError("DRAFT_CONTENT_INVALID")
    return normalized, {
        "required": False,
        "ok": True,
        "fact_count": 0,
        "hook_count": 0,
        "pillar_count": 0,
        "unmapped": [],
        "simple_profile_mode": True,
    }


def _validate_template(template: Dict[str, Any]):
    try:
        version = int(template.get("version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version < SIMPLE_PROFILE_DRAFT_VERSION:
        return _ORIGINAL_VALIDATE_TEMPLATE(template)

    normalized, coverage = _validate_simple_content(template)
    normalized = novel_drafts._validate_starting_state(normalized)
    return normalized, coverage


def _reconciliation_current(draft: Dict[str, Any]) -> bool:
    receipt = draft.get("reconciliation_receipt")
    if not isinstance(receipt, dict):
        return False
    revision = int(draft.get("revision", 0) or 0)
    if int(receipt.get("draft_revision", -1) or -1) != revision:
        return False
    if receipt.get("confirmed_against_raw") is not True:
        return False
    return str(receipt.get("draft_hash") or "") == canonical_hash({
        "revision": revision,
        "sections": draft.get("sections", {}),
    })


def _draft_status(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft):
        return dict(_ORIGINAL_DRAFT_STATUS(draft_id))

    sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
    required = ["novel", "characters", "intake"]
    missing = [name for name in required if name not in sections]
    characters = sections.get("characters", [])
    uploads = draft_intake_runtime._intake_upload_status(draft)
    intake_coverage = _simple_intake_coverage(draft)
    revision = int(draft.get("revision", 0) or 0)
    last_read = novel_access.completed_working_draft_revision(draft_id)
    full_read_current = last_read == revision

    blocker = None
    if uploads.get("pending_count"):
        blocker = "INTAKE_UPLOAD_INCOMPLETE"
    elif missing:
        blocker = "DRAFT_CONTENT_INCOMPLETE"
    else:
        try:
            _validate_simple_content(_content_template(draft))
        except ValueError as exc:
            blocker = str(exc)

    if blocker is None and not intake_coverage.get("ok"):
        blocker = "INTAKE_REVIEW_INCOMPLETE"
    if blocker is None and not full_read_current:
        blocker = "INTAKE_FINAL_READ_REQUIRED"
    if blocker is None and not _reconciliation_current(draft):
        blocker = "RECONCILIATION_REQUIRED"

    launch_ready, launch_blocker = setup_draft_v3_runtime._launch_state_status(draft)
    finalized = bool(draft.get("finalized"))

    return {
        "draft_id": draft_id,
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": int(draft.get("version", SIMPLE_PROFILE_DRAFT_VERSION) or SIMPLE_PROFILE_DRAFT_VERSION),
        "revision": revision,
        "saved_sections": sorted(sections.keys()),
        "missing_required_sections": missing,
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "ready_to_finalize": blocker is None,
        "finalize_blocker": blocker,
        "foundation_coverage": {
            "required": False,
            "ok": True,
            "simple_profile_mode": True,
        },
        "intake_coverage": {
            **draft_intake_runtime.coverage_for_response(intake_coverage),
            "draft_revision": revision,
            "last_full_read_revision": last_read,
            "full_read_current": full_read_current,
        },
        "intake_uploads": uploads,
        "reconciliation_current": _reconciliation_current(draft),
        "reconciliation_receipt": deepcopy(draft.get("reconciliation_receipt", {})),
        "finalized": finalized,
        "content_finalized": finalized,
        "launch_state_ready": bool(launch_ready),
        "launch_state_blocker": launch_blocker,
        "awaiting_launch_start": finalized and not launch_ready,
        "session_ready": finalized and launch_ready,
        "published_to_library": bool(draft.get("published_to_library")),
        "simple_profile_mode": True,
        "profile_schema": profile_manifest(),
    }


def _confirm_reconciliation(
    draft_id: str,
    *,
    expected_revision: int,
    confirmed_against_raw: bool,
    unresolved_conflicts: List[str],
    notes: str | None = None,
) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft):
        return _ORIGINAL_CONFIRM_RECONCILIATION(
            draft_id,
            expected_revision=expected_revision,
            confirmed_against_raw=confirmed_against_raw,
            unresolved_conflicts=unresolved_conflicts,
            notes=notes,
        )
    if not confirmed_against_raw:
        raise ValueError("RECONCILIATION_CONFIRMATION_REQUIRED")
    conflicts = [str(item).strip() for item in unresolved_conflicts if str(item).strip()]
    if conflicts:
        raise ValueError("RECONCILIATION_CONFLICTS_REMAIN")

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        revision = int(draft.get("revision", 0) or 0)
        if int(expected_revision) != revision:
            raise ValueError("RECONCILIATION_REVISION_MISMATCH")
        if novel_access.completed_working_draft_revision(draft_id) != revision:
            raise ValueError("RECONCILIATION_FULL_READ_REQUIRED")
        coverage = _simple_intake_coverage(draft)
        if not coverage.get("ok"):
            raise ValueError("INTAKE_REVIEW_INCOMPLETE")

        template, _ = _validate_simple_content(_content_template(draft))
        receipt = {
            "draft_revision": revision,
            "confirmed_against_raw": True,
            "draft_hash": canonical_hash({"revision": revision, "sections": draft.get("sections", {})}),
            "character_count": len(template.get("characters", [])),
            "simple_profile_mode": True,
        }
        if notes:
            receipt["notes"] = str(notes)[:1000]
        draft["reconciliation_receipt"] = receipt
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    result = dict(_draft_status(draft_id))
    result["reconciliation_confirmed"] = True
    return result


def _finalize_draft(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_simple_draft(draft):
        return dict(_ORIGINAL_FINALIZE(draft_id))

    status = _draft_status(draft_id)
    if not status.get("ready_to_finalize"):
        raise ValueError(status.get("finalize_blocker") or "DRAFT_INCOMPLETE")

    template, coverage = _validate_simple_content(_content_template(draft))
    verification = novel_drafts.verify_template(template)
    if not verification.get("ok"):
        raise RuntimeError("FINAL_VERIFICATION_FAILED")

    draft["finalized"] = True
    draft["finalized_revision"] = int(draft.get("revision", 0) or 0)
    draft["finalized_template"] = template
    novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    launch_ready, _ = setup_draft_v3_runtime._launch_state_status(draft)
    return {
        "ok": True,
        "draft_id": draft_id,
        "verification": verification,
        "foundation_coverage": coverage,
        "intake_coverage": status.get("intake_coverage"),
        "reconciliation_current": True,
        "content_finalized": True,
        "session_ready": bool(launch_ready),
        "awaiting_launch_start": not launch_ready,
        "saved_to_library": False,
        "simple_profile_mode": True,
        "instruction": "Профили полностью записаны и сверены с RAW. Следующая пользовательская команда: «запускай первую сцену».",
    }


def install() -> None:
    global _ORIGINAL_SAVE_SECTION, _ORIGINAL_DRAFT_STATUS, _ORIGINAL_FINALIZE
    global _ORIGINAL_VALIDATE_TEMPLATE, _ORIGINAL_APPEND_INTAKE, _ORIGINAL_UPDATE_MAPPING
    global _ORIGINAL_COVERAGE, _ORIGINAL_CONFIRM_RECONCILIATION, _ORIGINAL_SETUP_STATUS

    if _ORIGINAL_SAVE_SECTION is not None:
        return

    _ORIGINAL_SAVE_SECTION = novel_drafts.save_section
    _ORIGINAL_DRAFT_STATUS = novel_drafts.draft_status
    _ORIGINAL_FINALIZE = novel_drafts.finalize_draft
    _ORIGINAL_VALIDATE_TEMPLATE = novel_drafts._validate_template
    _ORIGINAL_APPEND_INTAKE = draft_intake_runtime.append_intake_chunk
    _ORIGINAL_UPDATE_MAPPING = draft_intake_runtime.update_intake_mapping
    _ORIGINAL_COVERAGE = draft_intake_runtime._coverage
    _ORIGINAL_CONFIRM_RECONCILIATION = setup_draft_v3_runtime.confirm_reconciliation
    _ORIGINAL_SETUP_STATUS = setup_draft_v3_runtime._draft_status

    novel_drafts.save_section = _save_section
    novel_drafts.draft_status = _draft_status
    novel_drafts.finalize_draft = _finalize_draft
    novel_drafts._validate_template = _validate_template

    draft_intake_runtime.append_intake_chunk = _append_intake_chunk
    draft_intake_runtime.update_intake_mapping = _update_intake_mapping
    draft_intake_runtime._coverage = _coverage

    setup_draft_v3_runtime.confirm_reconciliation = _confirm_reconciliation
    setup_draft_v3_runtime._draft_status = _draft_status
