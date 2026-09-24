from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, List

from . import draft_intake_runtime, novel_access, novel_drafts, setup_draft_v3_runtime
from .operation_receipts import canonical_hash
from .profile_templates import (
    normalize_character_profiles,
    normalize_hidden_lore,
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
    normalized = draft_intake_runtime._normalise_intake(intake)
    unreviewed = [
        str(row.get("block_id") or "")
        for row in normalized.get("blocks", [])
        if isinstance(row, dict) and row.get("reviewed_against_raw") is not True
    ]
    block_count = len(normalized.get("blocks", []))
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
    if name == "knowledge":
        # Knowledge starts empty. Runtime knowledge is learned during play.
        return {}
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
    return _ORIGINAL_SAVE_SECTION(
        draft_id,
        section_name,
        json.dumps(normalized, ensure_ascii=False),
        expected_revision=expected_revision,
    )


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
    sections.pop("intake", None)
    sections.pop("starting_state", None)

    characters = normalize_character_profiles(sections.pop("characters", []))
    novel = normalize_novel_profile(sections.pop("novel", {}), title=str(draft.get("title") or ""))
    novel = _ensure_core_cast(novel, characters)

    lore = sections.pop("lore", {})
    if not isinstance(lore, dict):
        lore = {"text": str(lore)} if lore not in (None, "") else {}

    hidden_lore = normalize_hidden_lore(sections.pop("hidden_lore", {}))
    sections.pop("knowledge", None)

    template: Dict[str, Any] = {
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": int(draft.get("version", SIMPLE_PROFILE_DRAFT_VERSION) or SIMPLE_PROFILE_DRAFT_VERSION),
        "novel": novel,
        "characters": characters,
        "lore": lore,
        "hidden_lore": hidden_lore,
        "knowledge": {},
        "profile_schema": profile_manifest(),
    }
    template.update(sections)
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


def _validate_simple_content(template: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    normalized = deepcopy(template)
    normalized["characters"] = normalize_character_profiles(normalized.get("characters", []))
    normalized["novel"] = _ensure_core_cast(
        normalize_novel_profile(normalized.get("novel", {}), title=str(normalized.get("title") or "")),
        normalized["characters"],
    )
    normalized["hidden_lore"] = normalize_hidden_lore(normalized.get("hidden_lore", {}))
    normalized["knowledge"] = {}

    if not normalized["characters"]:
        raise ValueError("DRAFT_CHARACTERS_REQUIRED")
    if not _resolve_pov(normalized):
        raise ValueError("DRAFT_POV_REQUIRED")

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
