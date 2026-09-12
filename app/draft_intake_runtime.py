from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, List

from . import novel_access, novel_drafts
from .transactional_storage import session_transaction


_ORIGINAL_SAVE_SECTION = None
_ORIGINAL_DRAFT_STATUS = None
_ORIGINAL_FINALIZE = None
_ORIGINAL_PREPARE_READ = None
_VERSION = 4
MAX_INTAKE_CHUNK_CHARS = 12000

_PLACEHOLDER_FRAGMENTS = (
    "[полный текст",
    "[полный дословный текст",
    "сохранён дословно в рабочем вводе",
    "сохранен дословно в рабочем вводе",
    "технический интерфейс не позволяет безопасно вставить весь объём",
    "технический интерфейс не позволяет безопасно вставить весь объем",
    "[full text",
    "[verbatim full text",
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_not_placeholder(raw_text: str) -> None:
    normalized = " ".join(str(raw_text or "").casefold().split())
    if any(fragment in normalized for fragment in _PLACEHOLDER_FRAGMENTS):
        raise ValueError("INTAKE_PLACEHOLDER_FORBIDDEN")


def _normalise_intake(value: Any, *, reject_placeholders: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("intake must be a JSON object")
    blocks = value.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("INTAKE_BLOCKS_REQUIRED")
    result = {"version": int(value.get("version", _VERSION) or _VERSION), "blocks": []}
    seen: set[str] = set()
    for raw in blocks:
        if not isinstance(raw, dict):
            raise ValueError("INTAKE_BLOCK_INVALID")
        block_id = str(raw.get("block_id") or "").strip()
        stage = str(raw.get("stage") or "").strip()
        raw_text = str(raw.get("raw_text") or "")
        fact_ids = raw.get("fact_ids")
        reviewed = bool(raw.get("reviewed_against_raw", False))
        if not block_id or block_id in seen or not stage or not raw_text.strip():
            raise ValueError("INTAKE_BLOCK_INVALID")
        if reject_placeholders:
            _assert_not_placeholder(raw_text)
        if not isinstance(fact_ids, list):
            raise ValueError("INTAKE_FACT_IDS_REQUIRED")
        fact_ids = [str(item).strip() for item in fact_ids if str(item).strip()]
        no_facts = bool(raw.get("contains_no_facts", False))
        # Raw-first chunked intake is allowed to exist unmapped only while it is unreviewed.
        if reviewed and not fact_ids and not no_facts:
            raise ValueError("INTAKE_FACT_IDS_REQUIRED")
        if no_facts and fact_ids:
            raise ValueError("INTAKE_FACT_IDS_CONFLICT")
        seen.add(block_id)
        result["blocks"].append({
            "block_id": block_id,
            "stage": stage,
            "raw_text": raw_text,
            "fact_ids": list(dict.fromkeys(fact_ids)),
            "reviewed_against_raw": reviewed,
            "contains_no_facts": no_facts,
        })
    return result


def _merge_intake(existing: Any, incoming: Any) -> Dict[str, Any]:
    old = _normalise_intake(existing) if isinstance(existing, dict) else {"version": _VERSION, "blocks": []}
    new = _normalise_intake(incoming, reject_placeholders=True)
    merged = {row["block_id"]: deepcopy(row) for row in old["blocks"]}
    order = [row["block_id"] for row in old["blocks"]]
    for row in new["blocks"]:
        block_id = row["block_id"]
        if block_id in merged:
            if merged[block_id]["raw_text"] != row["raw_text"] or merged[block_id]["stage"] != row["stage"]:
                raise ValueError("INTAKE_BLOCK_SOURCE_IMMUTABLE")
            if merged[block_id].get("contains_no_facts") and row["fact_ids"]:
                raise ValueError("INTAKE_FACT_IDS_CONFLICT")
            merged[block_id]["fact_ids"] = list(dict.fromkeys(merged[block_id]["fact_ids"] + row["fact_ids"]))
            merged[block_id]["reviewed_against_raw"] = bool(merged[block_id]["reviewed_against_raw"] or row["reviewed_against_raw"])
            merged[block_id]["contains_no_facts"] = bool(
                merged[block_id].get("contains_no_facts") or row.get("contains_no_facts")
            )
        else:
            merged[block_id] = deepcopy(row)
            order.append(block_id)
    return {
        "version": max(int(old.get("version", 1)), int(new.get("version", 1))),
        "blocks": [merged[key] for key in order],
    }


def _intake_upload_status(draft: Dict[str, Any]) -> Dict[str, Any]:
    uploads = draft.get("intake_uploads") if isinstance(draft.get("intake_uploads"), dict) else {}
    pending = []
    completed = []
    for block_id, raw in uploads.items():
        if not isinstance(raw, dict):
            continue
        row = {
            "block_id": str(block_id),
            "stage": str(raw.get("stage") or ""),
            "chunk_count": int(raw.get("chunk_count", len(raw.get("chunks", []))) or 0),
            "char_count": int(raw.get("char_count", sum(raw.get("lengths", []))) or 0),
        }
        if raw.get("completed"):
            completed.append(row)
        else:
            row["next_chunk_index"] = len(raw.get("chunks", [])) if isinstance(raw.get("chunks"), list) else 0
            pending.append(row)
    return {
        "pending": pending,
        "completed": completed,
        "pending_count": len(pending),
    }


def _coverage(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
    intake = sections.get("intake")
    if not isinstance(intake, dict):
        return {
            "required": False,
            "ok": True,
            "block_count": 0,
            "reviewed_blocks": 0,
            "unreviewed_blocks": [],
            "unknown_fact_ids": [],
        }
    intake = _normalise_intake(intake)
    foundation = sections.get("foundation") if isinstance(sections.get("foundation"), dict) else {}
    facts = foundation.get("facts") if isinstance(foundation.get("facts"), list) else []
    known = {str(row.get("fact_id")) for row in facts if isinstance(row, dict) and row.get("fact_id")}
    unreviewed = []
    unknown = []
    for block in intake["blocks"]:
        if not block["reviewed_against_raw"]:
            unreviewed.append(block["block_id"])
        for fact_id in block["fact_ids"]:
            if fact_id not in known:
                unknown.append({"block_id": block["block_id"], "fact_id": fact_id})
    return {
        "required": True,
        "ok": not unreviewed and not unknown,
        "block_count": len(intake["blocks"]),
        "reviewed_blocks": len(intake["blocks"]) - len(unreviewed),
        "unreviewed_blocks": unreviewed,
        "unknown_fact_ids": unknown,
        "instruction": "Every raw intake block is immutable, reviewed against its verbatim source, and mapped to existing foundation fact ids before finalize.",
    }


def append_intake_chunk(
    draft_id: str,
    *,
    block_id: str,
    stage: str,
    chunk_index: int,
    raw_text: str,
    is_last: bool,
) -> Dict[str, Any]:
    block_id = str(block_id or "").strip()
    stage = str(stage or "").strip()
    raw_text = str(raw_text or "")
    if not block_id or not stage or chunk_index < 0 or not raw_text:
        raise ValueError("INTAKE_UPLOAD_INVALID")
    if len(raw_text) > MAX_INTAKE_CHUNK_CHARS:
        raise ValueError("INTAKE_CHUNK_TOO_LARGE")
    _assert_not_placeholder(raw_text)

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        uploads = draft.get("intake_uploads")
        uploads = deepcopy(uploads) if isinstance(uploads, dict) else {}

        existing_blocks = {}
        existing_intake = draft.get("sections", {}).get("intake") if isinstance(draft.get("sections"), dict) else None
        if isinstance(existing_intake, dict):
            normalized = _normalise_intake(existing_intake)
            existing_blocks = {row["block_id"]: row for row in normalized["blocks"]}

        receipt = uploads.get(block_id)
        if receipt is None and block_id in existing_blocks:
            raise ValueError("INTAKE_UPLOAD_BLOCK_EXISTS")

        digest = _sha256(raw_text)
        if isinstance(receipt, dict) and receipt.get("completed"):
            hashes = receipt.get("hashes") if isinstance(receipt.get("hashes"), list) else []
            lengths = receipt.get("lengths") if isinstance(receipt.get("lengths"), list) else []
            chunk_count = int(receipt.get("chunk_count", len(hashes)) or 0)
            exact = (
                str(receipt.get("stage") or "") == stage
                and 0 <= chunk_index < chunk_count
                and chunk_index < len(hashes)
                and chunk_index < len(lengths)
                and str(hashes[chunk_index]) == digest
                and int(lengths[chunk_index]) == len(raw_text)
                and bool(is_last) == (chunk_index == chunk_count - 1)
            )
            if not exact:
                raise ValueError("INTAKE_UPLOAD_CHUNK_CONFLICT")
            return {
                "draft_id": draft_id,
                "block_id": block_id,
                "complete": True,
                "already_completed": True,
                "chunk_count": chunk_count,
                "char_count": int(receipt.get("char_count", sum(lengths)) or 0),
                "draft_revision": int(draft.get("revision", 0) or 0),
            }

        if not isinstance(receipt, dict):
            receipt = {
                "stage": stage,
                "chunks": [],
                "hashes": [],
                "lengths": [],
                "completed": False,
            }
        elif str(receipt.get("stage") or "") != stage:
            raise ValueError("INTAKE_UPLOAD_STAGE_IMMUTABLE")

        chunks = receipt.get("chunks")
        hashes = receipt.get("hashes")
        lengths = receipt.get("lengths")
        if not isinstance(chunks, list) or not isinstance(hashes, list) or not isinstance(lengths, list):
            raise ValueError("INTAKE_UPLOAD_CORRUPT")

        expected = len(chunks)
        if chunk_index < expected:
            exact = (
                chunk_index < len(hashes)
                and chunk_index < len(lengths)
                and str(hashes[chunk_index]) == digest
                and int(lengths[chunk_index]) == len(raw_text)
                and chunks[chunk_index] == raw_text
                and not is_last
            )
            if not exact:
                raise ValueError("INTAKE_UPLOAD_CHUNK_CONFLICT")
            return {
                "draft_id": draft_id,
                "block_id": block_id,
                "complete": False,
                "already_received": True,
                "next_chunk_index": expected,
                "char_count": sum(int(value) for value in lengths),
                "draft_revision": int(draft.get("revision", 0) or 0),
            }
        if chunk_index > expected:
            raise ValueError("INTAKE_UPLOAD_OUT_OF_ORDER")

        chunks.append(raw_text)
        hashes.append(digest)
        lengths.append(len(raw_text))
        receipt["chunks"] = chunks
        receipt["hashes"] = hashes
        receipt["lengths"] = lengths

        if not is_last:
            uploads[block_id] = receipt
            draft["intake_uploads"] = uploads
            novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
            return {
                "draft_id": draft_id,
                "block_id": block_id,
                "complete": False,
                "next_chunk_index": len(chunks),
                "char_count": sum(int(value) for value in lengths),
                "draft_revision": int(draft.get("revision", 0) or 0),
            }

        assembled = "".join(chunks)
        _assert_not_placeholder(assembled)
        incoming = {
            "version": _VERSION,
            "blocks": [{
                "block_id": block_id,
                "stage": stage,
                "raw_text": assembled,
                "fact_ids": [],
                "reviewed_against_raw": False,
                "contains_no_facts": False,
            }],
        }
        draft.setdefault("sections", {})["intake"] = _merge_intake(existing_intake, incoming)
        draft["revision"] = int(draft.get("revision", 0) or 0) + 1
        draft["finalized"] = False
        draft.pop("finalized_template", None)
        uploads[block_id] = {
            "stage": stage,
            "completed": True,
            "chunk_count": len(chunks),
            "char_count": len(assembled),
            "hashes": hashes,
            "lengths": lengths,
        }
        draft["intake_uploads"] = uploads
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
        revision = int(draft.get("revision", 0) or 0)

    return {
        "draft_id": draft_id,
        "block_id": block_id,
        "complete": True,
        "already_completed": False,
        "chunk_count": len(hashes),
        "char_count": len(assembled),
        "draft_revision": revision,
        "instruction": "Raw intake is stored verbatim. Create foundation facts, then map their existing fact_ids to this block without resending raw_text.",
    }


def update_intake_mapping(
    draft_id: str,
    block_id: str,
    *,
    fact_ids: List[str],
    reviewed_against_raw: bool,
    contains_no_facts: bool,
    replace: bool = False,
    expected_revision: int | None = None,
) -> Dict[str, Any]:
    block_id = str(block_id or "").strip()
    clean_fact_ids = list(dict.fromkeys(str(item).strip() for item in fact_ids if str(item).strip()))
    if not block_id:
        raise ValueError("INTAKE_BLOCK_INVALID")
    if contains_no_facts and clean_fact_ids:
        raise ValueError("INTAKE_FACT_IDS_CONFLICT")
    if reviewed_against_raw and not clean_fact_ids and not contains_no_facts:
        raise ValueError("INTAKE_FACT_IDS_REQUIRED")

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        revision_before = int(draft.get("revision", 0) or 0)
        if expected_revision is not None and int(expected_revision) != revision_before:
            raise ValueError("INTAKE_MAPPING_REVISION_MISMATCH")
        sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
        intake = sections.get("intake")
        if not isinstance(intake, dict):
            raise ValueError("INTAKE_BLOCK_NOT_FOUND")
        normalized = _normalise_intake(intake)
        blocks = normalized["blocks"]
        target = next((row for row in blocks if row["block_id"] == block_id), None)
        if target is None:
            raise ValueError("INTAKE_BLOCK_NOT_FOUND")

        foundation = sections.get("foundation") if isinstance(sections.get("foundation"), dict) else {}
        facts = foundation.get("facts") if isinstance(foundation.get("facts"), list) else []
        known = {str(row.get("fact_id")) for row in facts if isinstance(row, dict) and row.get("fact_id")}
        unknown = [fact_id for fact_id in clean_fact_ids if fact_id not in known]
        if unknown:
            raise ValueError("INTAKE_FACT_ID_UNKNOWN")

        before = deepcopy(target)
        if replace:
            target["fact_ids"] = clean_fact_ids
            target["reviewed_against_raw"] = bool(reviewed_against_raw)
            target["contains_no_facts"] = bool(contains_no_facts)
        else:
            if target.get("contains_no_facts") and clean_fact_ids:
                raise ValueError("INTAKE_FACT_IDS_CONFLICT")
            target["fact_ids"] = list(dict.fromkeys(target.get("fact_ids", []) + clean_fact_ids))
            target["reviewed_against_raw"] = bool(target.get("reviewed_against_raw") or reviewed_against_raw)
            target["contains_no_facts"] = bool(target.get("contains_no_facts") or contains_no_facts)
        if target["contains_no_facts"] and target["fact_ids"]:
            raise ValueError("INTAKE_FACT_IDS_CONFLICT")

        changed = target != before
        if changed:
            draft.setdefault("sections", {})["intake"] = {
                "version": max(int(normalized.get("version", 1)), _VERSION),
                "blocks": blocks,
            }
            draft["revision"] = int(draft.get("revision", 0) or 0) + 1
            draft["finalized"] = False
            draft.pop("finalized_template", None)
            novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
        revision = int(draft.get("revision", 0) or 0)

    result = dict(novel_drafts.draft_status(draft_id))
    result.update({
        "block_id": block_id,
        "mapping_changed": changed,
        "draft_revision": revision,
        "replace": bool(replace),
    })
    return result


def _save_section(draft_id: str, section_name: str, section_json: str) -> Dict[str, Any]:
    if section_name.strip() != "intake":
        return _ORIGINAL_SAVE_SECTION(draft_id, section_name, section_json)
    draft = novel_drafts._read(draft_id)
    was_finalized = bool(draft.get("finalized"))
    parsed = novel_drafts._parse_one_json(section_json)
    incoming = _normalise_intake(parsed, reject_placeholders=True)

    uploads = draft.get("intake_uploads") if isinstance(draft.get("intake_uploads"), dict) else {}
    for row in incoming["blocks"]:
        upload = uploads.get(row["block_id"])
        if isinstance(upload, dict) and not upload.get("completed"):
            raise ValueError("INTAKE_UPLOAD_IN_PROGRESS")

    merged = _merge_intake(draft.get("sections", {}).get("intake"), incoming)
    draft.setdefault("sections", {})["intake"] = merged
    draft["revision"] = int(draft.get("revision", 0) or 0) + 1
    draft["finalized"] = False
    draft.pop("finalized_template", None)
    novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
    result = dict(novel_drafts.draft_status(draft_id))
    result["reopened_from_finalized"] = was_finalized
    return result


def _draft_status(draft_id: str) -> Dict[str, Any]:
    result = dict(_ORIGINAL_DRAFT_STATUS(draft_id))
    draft = novel_drafts._read(draft_id)
    coverage = _coverage(draft)
    upload_status = _intake_upload_status(draft)
    revision = int(draft.get("revision", 0) or 0)
    last_full_read_revision = novel_access.completed_working_draft_revision(draft_id)
    coverage = deepcopy(coverage)
    coverage["draft_revision"] = revision
    coverage["last_full_read_revision"] = last_full_read_revision
    coverage["full_read_current"] = last_full_read_revision == revision
    result["intake_coverage"] = coverage
    result["intake_uploads"] = upload_status
    if upload_status["pending_count"]:
        result["ready_to_finalize"] = False
        result["finalize_blocker"] = "INTAKE_UPLOAD_INCOMPLETE"
    elif coverage["required"] and not coverage["ok"]:
        result["ready_to_finalize"] = False
        result["finalize_blocker"] = "INTAKE_COVERAGE_INCOMPLETE"
    elif coverage["required"] and not coverage["full_read_current"]:
        result["ready_to_finalize"] = False
        result["finalize_blocker"] = "INTAKE_FINAL_READ_REQUIRED"
    return result


def _finalize_draft(draft_id: str) -> Dict[str, Any]:
    status = _draft_status(draft_id)
    if not status.get("ready_to_finalize"):
        raise ValueError(status.get("finalize_blocker") or "DRAFT_INCOMPLETE")
    result = dict(_ORIGINAL_FINALIZE(draft_id))

    # Intake is an immutable setup audit trail, not gameplay canon transport.
    # Keep it in draft.sections so it can be inspected later, but do not copy the
    # potentially huge verbatim user history into source.json/library templates.
    draft = novel_drafts._read(draft_id)
    template = draft.get("finalized_template")
    if isinstance(template, dict) and "intake" in template:
        template = deepcopy(template)
        template.pop("intake", None)
        draft["finalized_template"] = template
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)

    result["intake_coverage"] = status.get("intake_coverage")
    result["intake_archived_in_draft_only"] = bool(status.get("intake_coverage", {}).get("required"))
    return result


def _prepare_draft_read(draft_id: str) -> Dict[str, Any]:
    draft = novel_drafts._read(draft_id)
    upload_status = _intake_upload_status(draft)
    if upload_status["pending_count"]:
        raise RuntimeError("INTAKE_UPLOAD_INCOMPLETE")
    if draft.get("finalized"):
        result = dict(_ORIGINAL_PREPARE_READ(draft_id))
        result["working_draft"] = False
        result["intake_archived_in_draft_only"] = isinstance(draft.get("sections", {}).get("intake"), dict)
        return result
    snapshot = {
        "draft_id": draft.get("draft_id"),
        "novel_id": draft.get("novel_id"),
        "title": draft.get("title"),
        "version": draft.get("version", 1),
        "revision": int(draft.get("revision", 0) or 0),
        "finalized": False,
        "sections": deepcopy(draft.get("sections", {})),
        "intake_coverage": _coverage(draft),
    }
    result = novel_drafts.prepare_template_read(
        snapshot,
        "draft_working",
        draft_id,
        source_revision=int(draft.get("revision", 0) or 0),
    )
    result["working_draft"] = True
    result["instruction"] = "Read every chunk in order. Finalization stays blocked until every chunk of the current draft revision is read. If this review finds any omission, save corrections and start a fresh full read of the new revision."
    return result


def install() -> None:
    global _ORIGINAL_SAVE_SECTION, _ORIGINAL_DRAFT_STATUS, _ORIGINAL_FINALIZE, _ORIGINAL_PREPARE_READ
    if _ORIGINAL_SAVE_SECTION is not None:
        return
    novel_drafts.ALLOWED_SECTIONS.add("intake")
    _ORIGINAL_SAVE_SECTION = novel_drafts.save_section
    _ORIGINAL_DRAFT_STATUS = novel_drafts.draft_status
    _ORIGINAL_FINALIZE = novel_drafts.finalize_draft
    _ORIGINAL_PREPARE_READ = novel_drafts.prepare_draft_read
    novel_drafts.save_section = _save_section
    novel_drafts.draft_status = _draft_status
    novel_drafts.finalize_draft = _finalize_draft
    novel_drafts.prepare_draft_read = _prepare_draft_read
