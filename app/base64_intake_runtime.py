"""Verified byte-exact RAW fallback for GPT Actions local-only TXT references.

Uses the EXISTING appendDraftIntakeChunk Action. The original UTF-8 file bytes
are base64-encoded in small parts by a caller that actually read the file.
Nothing enters the canonical intake until byte length and full-file SHA-256
are verified. Text-only and signed-file-ref flows are unchanged.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from copy import deepcopy
from typing import Any, Dict

from . import draft_intake_runtime, novel_drafts
from .transactional_storage import session_transaction


BYTE_PART_LIMIT = 12288
MAX_RAW_BYTES = 1_000_000
MAX_PARTS = (MAX_RAW_BYTES + BYTE_PART_LIMIT - 1) // BYTE_PART_LIMIT


def append_base64_intake_chunk(
    draft_id: str, *,
    block_id: str, stage: str, chunk_index: int, total_parts: int,
    chunk_b64: str, expected_bytes: int, expected_sha256: str,
) -> Dict[str, Any]:
    block_id = str(block_id or "").strip()
    stage = str(stage or "").strip()
    if (
        not block_id or not stage or not 1 <= expected_bytes <= MAX_RAW_BYTES
        or not 1 <= total_parts <= MAX_PARTS
        or chunk_index < 0 or chunk_index >= total_parts
        or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256))
        or not isinstance(chunk_b64, str)
    ):
        raise ValueError("RAW_BASE64_INVALID")
    try:
        binary = base64.b64decode(chunk_b64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("RAW_BASE64_INVALID") from None
    if not binary or len(binary) > BYTE_PART_LIMIT:
        raise ValueError("RAW_BASE64_PART_TOO_LARGE")
    if total_parts * BYTE_PART_LIMIT < expected_bytes:
        raise ValueError("RAW_BASE64_PART_COUNT_INVALID")
    part_digest = hashlib.sha256(binary).hexdigest()

    with session_transaction(novel_drafts._drafts_dir()):
        draft = novel_drafts._read(draft_id)
        uploads = deepcopy(draft.get("intake_uploads") or {})
        if not isinstance(uploads, dict):
            raise ValueError("INTAKE_UPLOAD_CORRUPT")
        intake = draft.get("sections", {}).get("intake")
        previous = draft_intake_runtime._normalise_intake(intake) if isinstance(intake, dict) else {"blocks": []}
        existing = {row["block_id"]: row for row in previous["blocks"]}
        receipt = uploads.get(block_id)
        if receipt is None and block_id in existing:
            raise ValueError("INTAKE_UPLOAD_BLOCK_EXISTS")
        if receipt is not None and (
            not isinstance(receipt, dict)
            or receipt.get("encoding") != "base64"
            or receipt.get("stage") != stage
            or receipt.get("total_parts") != total_parts
            or receipt.get("expected_bytes") != expected_bytes
            or receipt.get("expected_sha256") != expected_sha256
        ):
            raise ValueError("INTAKE_UPLOAD_CHUNK_CONFLICT")

        if receipt is not None and receipt.get("completed"):
            hashes = receipt.get("hashes") or []
            lengths = receipt.get("lengths") or []
            if (
                not isinstance(hashes, list) or not isinstance(lengths, list)
                or chunk_index >= len(hashes) or chunk_index >= len(lengths)
                or hashes[chunk_index] != part_digest
                or lengths[chunk_index] != len(binary)
            ):
                raise ValueError("INTAKE_UPLOAD_CHUNK_CONFLICT")
            content = existing.get(block_id)
            if not content or hashlib.sha256(content["raw_text"].encode("utf-8")).hexdigest() != expected_sha256:
                raise ValueError("INTAKE_UPLOAD_CORRUPT")
            return {
                "draft_id": draft_id, "block_id": block_id,
                "complete": True, "already_completed": True,
                "chunk_count": total_parts, "char_count": len(content["raw_text"]),
                "byte_count": expected_bytes, "raw_sha256": expected_sha256,
                "draft_revision": int(draft.get("revision", 0) or 0),
            }

        if receipt is None:
            receipt = {
                "encoding": "base64", "stage": stage, "completed": False,
                "total_parts": total_parts, "expected_bytes": expected_bytes,
                "expected_sha256": expected_sha256, "chunks_b64": [],
                "hashes": [], "lengths": [],
            }
        parts = receipt.get("chunks_b64")
        hashes = receipt.get("hashes")
        lengths = receipt.get("lengths")
        if not all(isinstance(row, list) for row in (parts, hashes, lengths)):
            raise ValueError("INTAKE_UPLOAD_CORRUPT")
        if len(parts) != len(hashes) or len(parts) != len(lengths):
            raise ValueError("INTAKE_UPLOAD_CORRUPT")
        received = len(parts)
        if chunk_index < received:
            if (
                parts[chunk_index] != chunk_b64 or hashes[chunk_index] != part_digest
                or lengths[chunk_index] != len(binary)
            ):
                raise ValueError("INTAKE_UPLOAD_CHUNK_CONFLICT")
            return {
                "draft_id": draft_id, "block_id": block_id,
                "complete": False, "already_received": True,
                "next_chunk_index": received, "received_parts": received,
                "total_parts": total_parts, "byte_count": sum(lengths),
                "expected_bytes": expected_bytes,
                "draft_revision": int(draft.get("revision", 0) or 0),
            }
        if chunk_index > received:
            raise ValueError("INTAKE_UPLOAD_OUT_OF_ORDER")

        parts.append(chunk_b64)
        hashes.append(part_digest)
        lengths.append(len(binary))
        if len(parts) < total_parts:
            uploads[block_id] = receipt
            draft["intake_uploads"] = uploads
            novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
            return {
                "draft_id": draft_id, "block_id": block_id,
                "complete": False, "next_chunk_index": len(parts),
                "received_parts": len(parts), "total_parts": total_parts,
                "byte_count": sum(lengths), "expected_bytes": expected_bytes,
                "draft_revision": int(draft.get("revision", 0) or 0),
                "instruction": "Continue immediately with next_chunk_index and exact next original-file bytes; no intermediate reply to the user.",
            }

        payload = b"".join(base64.b64decode(part, validate=True) for part in parts)
        if len(payload) != expected_bytes:
            raise ValueError("RAW_BASE64_LENGTH_MISMATCH")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("RAW_BASE64_SHA256_MISMATCH")
        try:
            raw_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("RAW_BASE64_NOT_UTF8") from None
        draft_intake_runtime._assert_not_placeholder(raw_text)

        incoming = {
            "version": draft_intake_runtime._VERSION,
            "blocks": [{
                "block_id": block_id, "stage": stage, "raw_text": raw_text,
                "fact_ids": [], "reviewed_against_raw": False,
                "contains_no_facts": False,
            }],
        }
        draft.setdefault("sections", {})["intake"] = draft_intake_runtime._merge_intake(intake, incoming)
        draft["revision"] = int(draft.get("revision", 0) or 0) + 1
        draft["finalized"] = False
        draft.pop("finalized_template", None)
        if int(draft.get("version", 1) or 1) >= 3:
            for key in ("launch_state", "launch_state_hash", "launch_hint",
                        "session_creation_receipt", "reconciliation_receipt"):
                draft.pop(key, None)
        uploads[block_id] = {
            "encoding": "base64", "stage": stage, "completed": True,
            "chunk_count": total_parts, "total_parts": total_parts,
            "expected_bytes": expected_bytes, "expected_sha256": expected_sha256,
            "char_count": len(raw_text), "hashes": list(hashes),
            "lengths": list(lengths),
        }
        draft["intake_uploads"] = uploads
        novel_drafts._write(novel_drafts._draft_path(draft_id), draft)
        revision = draft["revision"]

    return {
        "draft_id": draft_id, "block_id": block_id,
        "complete": True, "already_completed": False,
        "chunk_count": total_parts, "char_count": len(raw_text),
        "byte_count": expected_bytes, "raw_sha256": expected_sha256,
        "draft_revision": revision,
        "instruction": "Exact original-file bytes verified and saved. Reconcile all source facts, then finalize the current draft without intermediate user messages.",
    }
