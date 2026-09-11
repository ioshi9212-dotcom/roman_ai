from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from . import novel_access, novel_drafts


_ORIGINAL_SAVE_SECTION = None
_ORIGINAL_DRAFT_STATUS = None
_ORIGINAL_FINALIZE = None
_ORIGINAL_PREPARE_READ = None
_VERSION = 3


def _normalise_intake(value: Any) -> Dict[str, Any]:
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
        if not block_id or block_id in seen or not stage or not raw_text.strip():
            raise ValueError("INTAKE_BLOCK_INVALID")
        if not isinstance(fact_ids, list):
            raise ValueError("INTAKE_FACT_IDS_REQUIRED")
        fact_ids = [str(item).strip() for item in fact_ids if str(item).strip()]
        no_facts = bool(raw.get("contains_no_facts", False))
        if not fact_ids and not no_facts:
            raise ValueError("INTAKE_FACT_IDS_REQUIRED")
        seen.add(block_id)
        result["blocks"].append({
            "block_id": block_id,
            "stage": stage,
            "raw_text": raw_text,
            "fact_ids": list(dict.fromkeys(fact_ids)),
            "reviewed_against_raw": bool(raw.get("reviewed_against_raw", False)),
            "contains_no_facts": no_facts,
        })
    return result


def _merge_intake(existing: Any, incoming: Any) -> Dict[str, Any]:
    old = _normalise_intake(existing) if isinstance(existing, dict) else {"version": _VERSION, "blocks": []}
    new = _normalise_intake(incoming)
    merged = {row["block_id"]: deepcopy(row) for row in old["blocks"]}
    order = [row["block_id"] for row in old["blocks"]]
    for row in new["blocks"]:
        block_id = row["block_id"]
        if block_id in merged:
            if merged[block_id]["raw_text"] != row["raw_text"] or merged[block_id]["stage"] != row["stage"]:
                raise ValueError("INTAKE_BLOCK_SOURCE_IMMUTABLE")
            merged[block_id]["fact_ids"] = list(dict.fromkeys(merged[block_id]["fact_ids"] + row["fact_ids"]))
            merged[block_id]["reviewed_against_raw"] = bool(merged[block_id]["reviewed_against_raw"] or row["reviewed_against_raw"])
        else:
            merged[block_id] = deepcopy(row)
            order.append(block_id)
    return {"version": max(int(old.get("version", 1)), int(new.get("version", 1))), "blocks": [merged[key] for key in order]}


def _coverage(draft: Dict[str, Any]) -> Dict[str, Any]:
    sections = draft.get("sections") if isinstance(draft.get("sections"), dict) else {}
    intake = sections.get("intake")
    if not isinstance(intake, dict):
        return {"required": False, "ok": True, "block_count": 0, "reviewed_blocks": 0, "unreviewed_blocks": [], "unknown_fact_ids": []}
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


def _save_section(draft_id: str, section_name: str, section_json: str) -> Dict[str, Any]:
    if section_name.strip() != "intake":
        return _ORIGINAL_SAVE_SECTION(draft_id, section_name, section_json)
    draft = novel_drafts._read(draft_id)
    was_finalized = bool(draft.get("finalized"))
    parsed = novel_drafts._parse_one_json(section_json)
    merged = _merge_intake(draft.get("sections", {}).get("intake"), parsed)
    draft.setdefault("sections", {})["intake"] = merged
    draft["revision"] = int(draft.get("revision", 0) or 0) + 1
    draft["finalized"] = False
    draft.pop("finalized_template", None)
    novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
    result = dict(_draft_status(draft_id))
    result["reopened_from_finalized"] = was_finalized
    return result


def _draft_status(draft_id: str) -> Dict[str, Any]:
    result = dict(_ORIGINAL_DRAFT_STATUS(draft_id))
    draft = novel_drafts._read(draft_id)
    coverage = _coverage(draft)
    revision = int(draft.get("revision", 0) or 0)
    last_full_read_revision = novel_access.completed_working_draft_revision(draft_id)
    coverage = deepcopy(coverage)
    coverage["draft_revision"] = revision
    coverage["last_full_read_revision"] = last_full_read_revision
    coverage["full_read_current"] = last_full_read_revision == revision
    result["intake_coverage"] = coverage
    if coverage["required"] and not coverage["ok"]:
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
