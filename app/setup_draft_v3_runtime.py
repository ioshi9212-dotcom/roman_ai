from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any, Dict, List

from . import draft_intake_runtime, novel_access, novel_drafts, storage
from .operation_receipts import canonical_hash
from .transactional_storage import session_transaction


_ORIGINAL_DRAFT_STATUS = None
_ORIGINAL_FINALIZE = None
_ORIGINAL_CREATE_SESSION = None
_ORIGINAL_PUBLISH = None
_VERSION = 3


def _is_v3(draft: Dict[str, Any]) -> bool:
    return int(draft.get("version", 1) or 1) >= _VERSION


def _content_template(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = deepcopy(draft.get("sections", {})) if isinstance(draft.get("sections"), dict) else {}
    sections.pop("intake", None)
    sections.pop("starting_state", None)
    template = {
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": int(draft.get("version", _VERSION) or _VERSION),
        "novel": sections.pop("novel", {}),
        "characters": sections.pop("characters", []),
        "lore": sections.pop("lore", {}),
    }
    template.update(sections)
    return novel_drafts._normalise_foundation_shape(template)


def _pov_id(template: Dict[str, Any]) -> str | None:
    cards = storage._normalise_cards(template.get("characters", []))
    inferred = storage._find_pov_id(template, cards)
    return str(inferred) if inferred else None


def _validate_content(template: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    normalized = novel_drafts._normalise_foundation_shape(template)
    verification = novel_drafts.verify_template(normalized)
    if not verification.get("ok"):
        raise ValueError("DRAFT_CONTENT_INVALID")
    if not _pov_id(normalized):
        raise ValueError("DRAFT_POV_REQUIRED")
    coverage = novel_drafts._foundation_coverage(normalized, required=True)
    return normalized, coverage


def _reconciliation_receipt(draft: Dict[str, Any]) -> Dict[str, Any]:
    raw = draft.get("reconciliation_receipt")
    return deepcopy(raw) if isinstance(raw, dict) else {}


def _reconciliation_current(draft: Dict[str, Any]) -> bool:
    receipt = _reconciliation_receipt(draft)
    try:
        receipt_revision = int(receipt.get("draft_revision", -1))
        current_revision = int(draft.get("revision", 0) or 0)
    except (TypeError, ValueError):
        return False
    if receipt_revision != current_revision or receipt.get("confirmed_against_raw") is not True:
        return False
    return str(receipt.get("draft_hash") or "") == canonical_hash({
        "revision": current_revision,
        "sections": draft.get("sections", {}),
    })


def confirm_reconciliation(
    draft_id: str,
    *,
    expected_revision: int,
    confirmed_against_raw: bool,
    unresolved_conflicts: List[str],
    notes: str | None = None,
) -> Dict[str, Any]:
    if not confirmed_against_raw:
        raise ValueError("RECONCILIATION_CONFIRMATION_REQUIRED")
    conflicts = [str(item).strip() for item in unresolved_conflicts if str(item).strip()]
    if conflicts:
        raise ValueError("RECONCILIATION_CONFLICTS_REMAIN")

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        if not _is_v3(draft):
            raise ValueError("RECONCILIATION_V3_REQUIRED")
        revision = int(draft.get("revision", 0) or 0)
        if int(expected_revision) != revision:
            raise ValueError("RECONCILIATION_REVISION_MISMATCH")
        last_read = novel_access.completed_working_draft_revision(draft_id)
        if last_read != revision:
            raise ValueError("RECONCILIATION_FULL_READ_REQUIRED")

        intake = draft.get("sections", {}).get("intake") if isinstance(draft.get("sections"), dict) else None
        if not isinstance(intake, dict):
            raise ValueError("INTAKE_REQUIRED")
        coverage = draft_intake_runtime._coverage(draft)
        if not coverage.get("ok"):
            raise ValueError("INTAKE_COVERAGE_INCOMPLETE")

        template, foundation_coverage = _validate_content(_content_template(draft))
        receipt = {
            "draft_revision": revision,
            "confirmed_against_raw": True,
            "draft_hash": canonical_hash({"revision": revision, "sections": draft.get("sections", {})}),
            "fact_count": int(foundation_coverage.get("fact_count", 0) or 0),
            "character_count": len(template.get("characters", [])) if isinstance(template.get("characters"), list) else 0,
        }
        if notes:
            receipt["notes"] = str(notes)[:1000]
        draft["reconciliation_receipt"] = receipt
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    result = dict(_draft_status(draft_id))
    result["reconciliation_confirmed"] = True
    return result


def _launch_state_status(draft: Dict[str, Any]) -> tuple[bool, str | None]:
    raw = draft.get("launch_state")
    if not isinstance(raw, dict):
        return False, "LAUNCH_STATE_REQUIRED"
    try:
        template = deepcopy(draft.get("finalized_template", {}))
        if not isinstance(template, dict) or not template:
            return False, "DRAFT_NOT_FINALIZED"
        template["starting_state"] = deepcopy(raw)
        novel_drafts._validate_template(template)
    except ValueError as exc:
        return False, str(exc)
    return True, None


def set_launch_state(
    draft_id: str,
    *,
    expected_finalized_revision: int,
    starting_state_json: str,
    launch_hint: str | None = None,
) -> Dict[str, Any]:
    try:
        raw_state = novel_drafts._parse_one_json(starting_state_json)
    except Exception as exc:
        raise ValueError("LAUNCH_STATE_JSON_INVALID") from exc
    if not isinstance(raw_state, dict):
        raise ValueError("LAUNCH_STATE_JSON_INVALID")

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        if not _is_v3(draft):
            raise ValueError("LAUNCH_STATE_V3_REQUIRED")
        if not draft.get("finalized") or not isinstance(draft.get("finalized_template"), dict):
            raise ValueError("DRAFT_NOT_FINALIZED")
        finalized_revision = int(draft.get("finalized_revision", -1) or -1)
        if int(expected_finalized_revision) != finalized_revision:
            raise ValueError("LAUNCH_REVISION_MISMATCH")

        template = deepcopy(draft["finalized_template"])
        template["starting_state"] = raw_state
        normalized, _coverage = novel_drafts._validate_template(template)
        launch_state = deepcopy(normalized["starting_state"])
        launch_hash = canonical_hash(launch_state)

        changed = launch_hash != str(draft.get("launch_state_hash") or "")
        draft["launch_state"] = launch_state
        draft["launch_state_hash"] = launch_hash
        draft["launch_state_revision"] = int(draft.get("launch_state_revision", 0) or 0) + (1 if changed else 0)
        if launch_hint not in (None, ""):
            draft["launch_hint"] = str(launch_hint)[:2000]
        if changed:
            draft.pop("session_creation_receipt", None)
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    result = dict(_draft_status(draft_id))
    result["launch_state_changed"] = changed
    result["launch_state_hash"] = launch_hash
    return result


def _draft_status(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_v3(draft):
        return dict(_ORIGINAL_DRAFT_STATUS(draft_id))

    sections = draft.get("sections", {}) if isinstance(draft.get("sections"), dict) else {}
    characters = sections.get("characters", [])
    required = ["novel", "characters", "lore", "foundation", "intake"]
    missing = [name for name in required if name not in sections]
    blocker = None
    foundation_coverage = None

    uploads = draft_intake_runtime._intake_upload_status(draft)
    intake_coverage = draft_intake_runtime._coverage(draft)
    revision = int(draft.get("revision", 0) or 0)
    last_read = novel_access.completed_working_draft_revision(draft_id)
    full_read_current = last_read == revision

    if uploads.get("pending_count"):
        blocker = "INTAKE_UPLOAD_INCOMPLETE"
    elif missing:
        blocker = "DRAFT_CONTENT_INCOMPLETE"
    else:
        try:
            _template, foundation_coverage = _validate_content(_content_template(draft))
        except ValueError as exc:
            blocker = str(exc)

    if blocker is None and not intake_coverage.get("ok"):
        blocker = "INTAKE_COVERAGE_INCOMPLETE"
    if blocker is None and not full_read_current:
        blocker = "INTAKE_FINAL_READ_REQUIRED"
    if blocker is None and not _reconciliation_current(draft):
        blocker = "RECONCILIATION_REQUIRED"

    launch_ready, launch_blocker = _launch_state_status(draft)
    finalized = bool(draft.get("finalized"))

    return {
        "draft_id": draft_id,
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": int(draft.get("version", _VERSION) or _VERSION),
        "revision": revision,
        "saved_sections": sorted(sections.keys()),
        "missing_required_sections": missing,
        "character_count": len(characters) if isinstance(characters, list) else 0,
        "ready_to_finalize": blocker is None,
        "finalize_blocker": blocker,
        "foundation_coverage": foundation_coverage,
        "intake_coverage": {
            **deepcopy(intake_coverage),
            "draft_revision": revision,
            "last_full_read_revision": last_read,
            "full_read_current": full_read_current,
        },
        "intake_uploads": uploads,
        "reconciliation_current": _reconciliation_current(draft),
        "reconciliation_receipt": _reconciliation_receipt(draft),
        "finalized": finalized,
        "content_finalized": finalized,
        "launch_state_ready": bool(launch_ready),
        "launch_state_blocker": launch_blocker,
        "awaiting_launch_start": finalized and not launch_ready,
        "session_ready": finalized and launch_ready,
        "published_to_library": bool(draft.get("published_to_library")),
    }


def _finalize_draft(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_v3(draft):
        return dict(_ORIGINAL_FINALIZE(draft_id))

    status = _draft_status(draft_id)
    if not status.get("ready_to_finalize"):
        raise ValueError(status.get("finalize_blocker") or "DRAFT_INCOMPLETE")

    template, coverage = _validate_content(_content_template(draft))
    verification = novel_drafts.verify_template(template)
    if not verification.get("ok"):
        raise RuntimeError("FINAL_VERIFICATION_FAILED")

    draft["finalized"] = True
    draft["finalized_revision"] = int(draft.get("revision", 0) or 0)
    draft["finalized_template"] = template
    novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    launch_ready, _ = _launch_state_status(draft)
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
        "instruction": (
            "Content is sealed and reconciled against verbatim intake. Do not create a session yet unless launch_state_ready=true. "
            "On the user's launch command choose a causal first-scene starting_state from canon/hint, call setDraftLaunchState, then createSessionFromDraft."
        ),
    }


def _create_session_from_draft(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_v3(draft):
        return dict(_ORIGINAL_CREATE_SESSION(draft_id))

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        template = draft.get("finalized_template")
        if not draft.get("finalized") or not isinstance(template, dict):
            raise RuntimeError("DRAFT_NOT_FINALIZED")
        launch_state = draft.get("launch_state")
        if not isinstance(launch_state, dict):
            raise RuntimeError("LAUNCH_STATE_REQUIRED")

        template = deepcopy(template)
        template["starting_state"] = deepcopy(launch_state)
        template, coverage = novel_drafts._validate_template(template)
        revision = int(draft.get("finalized_revision", draft.get("revision", 0)) or 0)
        template_hash = canonical_hash(template)
        launch_hash = canonical_hash(launch_state)

        receipt = draft.get("session_creation_receipt")
        if isinstance(receipt, dict):
            session_id = str(receipt.get("session_id") or "")
            if (
                int(receipt.get("draft_revision", -1) or -1) == revision
                and str(receipt.get("template_hash") or "") == template_hash
                and str(receipt.get("launch_state_hash") or "") == launch_hash
                and session_id
                and (storage.SESSIONS_DIR / session_id).exists()
            ):
                existing = storage._read_json(storage.SESSIONS_DIR / session_id / "meta.json", {})
                if isinstance(existing, dict) and existing.get("session_id") == session_id:
                    result = deepcopy(existing)
                    result["already_created"] = True
                    result["idempotent_replay"] = True
                    return result

        existing = novel_drafts._existing_session_for_draft(draft_id, revision, template)
        if existing is not None:
            draft["session_creation_receipt"] = {
                "draft_revision": revision,
                "template_hash": template_hash,
                "launch_state_hash": launch_hash,
                "session_id": existing["session_id"],
            }
            novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
            return existing

        deterministic_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"roman-ai:draft-v3:{draft_id}:revision:{revision}:launch:{launch_hash}:template:{template_hash}",
        ).hex
        meta = storage.create_session(
            template,
            session_id=deterministic_id,
            meta_patch={
                "source_type": "session_draft_v3",
                "source_draft_id": draft_id,
                "source_draft_revision": revision,
                "source_draft_hash": template_hash,
                "launch_state_hash": launch_hash,
                "foundation_coverage": coverage,
            },
        )
        draft["session_creation_receipt"] = {
            "draft_revision": revision,
            "template_hash": template_hash,
            "launch_state_hash": launch_hash,
            "session_id": meta["session_id"],
        }
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
        result = deepcopy(meta)
        result["already_created"] = False
        result["idempotent_replay"] = False
        return result


def _publish_draft_to_library(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    if not _is_v3(draft):
        return dict(_ORIGINAL_PUBLISH(draft_id))
    if not draft.get("finalized") or not isinstance(draft.get("finalized_template"), dict):
        raise RuntimeError("DRAFT_NOT_FINALIZED")
    launch_state = draft.get("launch_state")
    if not isinstance(launch_state, dict):
        raise RuntimeError("LAUNCH_STATE_REQUIRED")

    template = deepcopy(draft["finalized_template"])
    template["starting_state"] = deepcopy(launch_state)
    template, _coverage = novel_drafts._validate_template(template)
    storage.save_novel(template)
    draft["published_to_library"] = True
    novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
    return {
        "ok": True,
        "novel_id": template["novel_id"],
        "title": template["title"],
        "published_to_library": True,
    }


def install() -> None:
    global _ORIGINAL_DRAFT_STATUS, _ORIGINAL_FINALIZE, _ORIGINAL_CREATE_SESSION, _ORIGINAL_PUBLISH
    if _ORIGINAL_DRAFT_STATUS is not None:
        return
    _ORIGINAL_DRAFT_STATUS = novel_drafts.draft_status
    _ORIGINAL_FINALIZE = novel_drafts.finalize_draft
    _ORIGINAL_CREATE_SESSION = novel_drafts.create_session_from_draft
    _ORIGINAL_PUBLISH = novel_drafts.publish_draft_to_library
    novel_drafts.draft_status = _draft_status
    novel_drafts.finalize_draft = _finalize_draft
    novel_drafts.create_session_from_draft = _create_session_from_draft
    novel_drafts.publish_draft_to_library = _publish_draft_to_library
