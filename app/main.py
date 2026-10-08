import json
import logging
import os
from threading import Thread

from fastapi import FastAPI, HTTPException

from .audit_runtime import get_audit_snapshot, get_audit_snapshot_chunk
from . import commit_failure_diagnostics, session_checkpoint
from .character_access import get_character_bundle
from .character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read
from .context_stats import session_context_stats
from .continuation_runtime import build_continuation_preview, commit_continuation_block, commit_continuation_final, create_continuation_session, get_continuation_read_chunk, prepare_continuation_block_read, prepare_continuation_compaction, prepare_continuation_final_read
from .models import AuditCommit, CommitTurnRequest, ContinuationBlockCommit, ContinuationFinalCommit, NovelDraftCreate, NovelDraftIntakeChunk, NovelDraftIntakeMapping, NovelDraftLaunchState, NovelDraftReconciliation, NovelDraftSection, NovelRawSave, NovelTemplate, RollbackLastTurn, SceneArchiveRead, SessionCreate, TurnCommit, TurnPrepare
from .novel_access import get_novel_read_chunk, prepare_novel_read, verify_novel
from .novel_drafts import (
    create_draft,
    create_session_from_draft,
    draft_status,
    finalize_draft,
    prepare_draft_read,
    publish_draft_to_library,
    save_section,
)
from .draft_intake_runtime import append_intake_chunk, update_intake_mapping
from .setup_draft_v3_runtime import confirm_reconciliation, set_launch_state
from .runtime_access import runtime_chunk, runtime_manifest
from .session_preview import get_session_preview
from .session_recovery import recover_session_current
from .session_runtime import continue_session, prepare_turn_packet
from .scene_archive_read import get_scene_archive_chunk, prepare_scene_archive_read
from .scene_knowledge_read import (
    get_character_knowledge_chunk,
    prepare_character_knowledge_read,
    scene_knowledge_read_status,
)
from .operation_service import (
    OperationReceiptConflict,
    commit_audit_request,
    commit_turn_request,
    pending_turn_status,
    prepare_turn_request,
    rollback_last_turn_request,
)
from .storage import (
    create_session,
    get_character_memory,
    get_novel,
    get_turn_packet_chunk,
    get_turn_range,
    list_novels,
    load_session,
    save_novel,
)
from .turn_rollback import RollbackError
from .startup_migration import read_migration_status, run_startup_session_migration

app = FastAPI(
    title="Roman AI",
    version="1.17.0",
    description="Persistent interactive-novel sessions with rules-driven scenes, character-scoped knowledge, dynamic relationships, cast continuity, recovery, rollback and lossless setup intake.",
)


@app.on_event("startup")
def startup_session_migration():
    if str(os.getenv("ROMAN_MIGRATE_SESSION_ID") or "").strip():
        Thread(target=run_startup_session_migration, daemon=True, name="roman-session-migration").start()


@app.get("/health", operation_id="health", include_in_schema=False)
def health():
    return {"ok": True, "resume_checkpoint_version": session_checkpoint.CHECKPOINT_VERSION}


@app.get("/migration-status", operation_id="getMigrationStatus", include_in_schema=False)
def migration_status_get():
    return read_migration_status()


@app.get("/sessions/{session_id}/context-stats", operation_id="getSessionContextStats", include_in_schema=False)
def session_context_stats_get(session_id: str):
    try:
        return session_context_stats(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/runtime", operation_id="getRuntime", include_in_schema=False)
def runtime_get():
    return runtime_manifest()


@app.get("/runtime/{chunk_index}", operation_id="getRuntimeChunk", include_in_schema=False)
def runtime_chunk_get(chunk_index: int):
    try:
        return runtime_chunk(chunk_index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Runtime chunk index out of range")


@app.post("/novel-drafts", operation_id="createNovelDraft")
def novel_draft_create(body: NovelDraftCreate):
    return create_draft(body.novel_id, body.title, body.version)


@app.post("/novel-drafts/{draft_id}/sections", operation_id="saveNovelDraftSection")
def novel_draft_section_save(draft_id: str, body: NovelDraftSection):
    try:
        return save_section(
            draft_id,
            body.section_name,
            body.section_json,
            expected_revision=body.expected_revision,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except KeyError:
        raise HTTPException(status_code=422, detail="Unknown section_name")
    except (ValueError, TypeError) as exc:
        if str(exc) in {"DRAFT_SECTION_REVISION_REQUIRED", "DRAFT_SECTION_REVISION_MISMATCH"}:
            raise HTTPException(status_code=409, detail=str(exc))
        if str(exc) == "INTAKE_BLOCK_SOURCE_IMMUTABLE":
            raise HTTPException(
                status_code=409,
                detail=(
                    "This intake block_id already exists with different raw_text or stage. "
                    "For new material, create a new unique block_id and send only that new block. "
                    "If you only need to add fact_ids to an existing block, reuse its exact raw_text "
                    "and stage from the current draft; never reconstruct them from memory."
                ),
            )
        raise HTTPException(status_code=422, detail=f"Invalid section_json: {exc}")


@app.post("/novel-drafts/{draft_id}/intake/chunks", operation_id="appendDraftIntakeChunk")
def novel_draft_intake_chunk_append(draft_id: str, body: NovelDraftIntakeChunk):
    try:
        return append_intake_chunk(
            draft_id,
            block_id=body.block_id,
            stage=body.stage,
            chunk_index=body.chunk_index,
            raw_text=body.raw_text,
            is_last=body.is_last,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        code = str(exc)
        if code == "INTAKE_PLACEHOLDER_FORBIDDEN":
            raise HTTPException(
                status_code=422,
                detail="raw_text must contain the user's verbatim text, never a placeholder, summary, reference to another message, or '[full text]' marker.",
            )
        if code == "INTAKE_CHUNK_TOO_LARGE":
            raise HTTPException(status_code=422, detail="raw_text chunk exceeds the 100000-character backend safety ceiling. Keep the same block_id/stage and split only the unsaved verbatim text into smaller consecutive chunks; do not ask the user to resend it.")
        messages = {
            "INTAKE_UPLOAD_BLOCK_EXISTS": "This block_id already exists outside this upload. Use a new unique block_id.",
            "INTAKE_UPLOAD_STAGE_IMMUTABLE": "stage changed during the same intake upload. Retry with the exact original stage.",
            "INTAKE_UPLOAD_CHUNK_CONFLICT": "This chunk_index was already received with different content or is_last. Retry with the exact same chunk or continue with next_chunk_index.",
            "INTAKE_UPLOAD_OUT_OF_ORDER": "Chunks must be sent in order starting at 0. Continue with next_chunk_index from the previous response.",
            "INTAKE_UPLOAD_CORRUPT": "Stored intake upload state is inconsistent; do not reconstruct raw text from memory.",
            "INTAKE_UPLOAD_INVALID": "block_id, stage and raw_text are required and chunk_index must be >= 0.",
        }
        raise HTTPException(status_code=409, detail=messages.get(code, code))


@app.post("/novel-drafts/{draft_id}/intake/{block_id}/mapping", operation_id="updateDraftIntakeMapping")
def novel_draft_intake_mapping_update(draft_id: str, block_id: str, body: NovelDraftIntakeMapping):
    try:
        return update_intake_mapping(
            draft_id,
            block_id,
            fact_ids=body.fact_ids,
            reviewed_against_raw=body.reviewed_against_raw,
            contains_no_facts=body.contains_no_facts,
            replace=body.replace,
            expected_revision=body.expected_revision,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        code = str(exc)
        messages = {
            "INTAKE_BLOCK_NOT_FOUND": "The intake block does not exist. Finish its chunked raw upload first.",
            "INTAKE_FACT_ID_UNKNOWN": "One or more fact_ids do not exist in foundation yet. Save the foundation facts first, then map them.",
            "INTAKE_FACT_IDS_REQUIRED": "A reviewed factual block must map to at least one existing foundation fact_id.",
            "INTAKE_FACT_IDS_CONFLICT": "contains_no_facts cannot be combined with fact_ids.",
            "INTAKE_BLOCK_INVALID": "block_id is required.",
            "INTAKE_MAPPING_REVISION_REQUIRED": "replace=true requires expected_revision so a stale correction cannot overwrite a newer mapping.",
            "INTAKE_MAPPING_REVISION_MISMATCH": "The draft changed since this mapping was prepared. Read the current draft revision before replacing mappings.",
            "INTAKE_SOURCE_UNITS_REQUIRE_FACTS": "This v4 block contains substantive source_units and cannot be marked contains_no_facts. Map every source unit to foundation facts.",
        }
        if code.startswith("INTAKE_SOURCE_UNITS_UNCOVERED:"):
            missing = code.split(":", 1)[1]
            raise HTTPException(
                status_code=409,
                detail=f"Lossless v4 review failed. These source_unit_ids are still not represented by mapped foundation facts: {missing}. Add/correct facts with source_unit_ids before reviewed_against_raw=true.",
            )
        raise HTTPException(status_code=409, detail=messages.get(code, code))


@app.get("/novel-drafts/{draft_id}", operation_id="getNovelDraftStatus", include_in_schema=False)
def novel_draft_status_get(draft_id: str):
    try:
        return draft_status(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")


@app.post("/novel-drafts/{draft_id}/reconciliation", operation_id="confirmDraftReconciliation")
def novel_draft_reconciliation_confirm(draft_id: str, body: NovelDraftReconciliation):
    try:
        return confirm_reconciliation(
            draft_id,
            expected_revision=body.expected_revision,
            confirmed_against_raw=body.confirmed_against_raw,
            unresolved_conflicts=body.unresolved_conflicts,
            notes=body.notes,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/novel-drafts/{draft_id}/launch-state", operation_id="setDraftLaunchState")
def novel_draft_launch_state_set(draft_id: str, body: NovelDraftLaunchState):
    try:
        return set_launch_state(
            draft_id,
            expected_finalized_revision=body.expected_finalized_revision,
            starting_state_json=body.starting_state_json,
            launch_hint=body.launch_hint,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/novel-drafts/{draft_id}/finalize", operation_id="finalizeNovelDraft")
def novel_draft_finalize(draft_id: str):
    try:
        return finalize_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError as exc:
        code = str(exc)
        core_errors = {
            "CORE_CAST_REQUIRED": "Draft v3 requires novel.core_cast with the main player-defined cast.",
            "CORE_CAST_ROW_INVALID": "Every novel.core_cast item must contain character_id/name and story_function.",
            "CORE_CAST_CHARACTER_UNKNOWN": "novel.core_cast references a character that is missing from characters.",
            "CORE_CAST_CHARACTER_DUPLICATE": "novel.core_cast contains the same character more than once.",
            "CORE_CAST_STORY_FUNCTION_REQUIRED": "Every core cast member needs one short director-level story_function explaining why they matter to the plot.",
            "CORE_CAST_STORY_FUNCTION_TOO_LONG": "core_cast story_function must stay short (max 360 characters).",
            "CORE_CAST_POV_REQUIRED": "The POV character must be included in novel.core_cast.",
            "DRAFT_LOCATION_ID_DUPLICATE": "Location profiles must use unique location_id values.",
            "DRAFT_LOCATION_ZONE_ID_DUPLICATE": "Zones inside one location profile must use unique zone_id values.",
            "DRAFT_LOCATION_CHARACTER_UNKNOWN": "A linked location character must reference an existing character.",
            "DRAFT_LOCATION_CHARACTER_AMBIGUOUS": "A linked location character name matches more than one existing character; use character_id.",
        }
        raise HTTPException(status_code=409, detail=core_errors.get(code, "Draft is incomplete"))
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Final draft verification failed")


@app.post("/novel-drafts/{draft_id}/read", operation_id="prepareDraftRead")
def novel_draft_read_prepare(draft_id: str):
    try:
        return prepare_draft_read(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Draft read is not available")


@app.post("/novel-drafts/{draft_id}/session", operation_id="createSessionFromDraft")
def novel_draft_session_create(draft_id: str):
    try:
        return create_session_from_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except RuntimeError as exc:
        code = str(exc)
        if code == "LAUNCH_STATE_REQUIRED":
            raise HTTPException(status_code=409, detail="Draft content is finalized, but launch state is not set yet. Use setDraftLaunchState on the user's launch command.")
        raise HTTPException(status_code=409, detail="Draft must be finalized first")


@app.post("/novel-drafts/{draft_id}/publish", operation_id="saveDraftToLibrary")
def novel_draft_publish(draft_id: str):
    try:
        return publish_draft_to_library(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except RuntimeError as exc:
        if str(exc) == "LAUNCH_STATE_REQUIRED":
            raise HTTPException(status_code=409, detail="Draft v3 must have a validated launch state before publishing to the reusable library.")
        raise HTTPException(status_code=409, detail="Draft must be finalized first")


@app.get("/novels", operation_id="listNovels")
def novels_list():
    return {"novels": list_novels()}


@app.post("/novels", operation_id="saveNovel", include_in_schema=False)
def novels_save(template: NovelTemplate):
    return save_novel(template.model_dump())


@app.post("/novels/raw", operation_id="saveNovelRaw", include_in_schema=False)
def novels_save_raw(body: NovelRawSave):
    try:
        template = json.loads(body.template_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid template_json: {exc.msg}")
    if not isinstance(template, dict):
        raise HTTPException(status_code=422, detail="template_json must decode to an object")
    if not template.get("novel_id") or not template.get("title"):
        raise HTTPException(status_code=422, detail="novel_id and title are required")
    return save_novel(template)


@app.get("/novels/{novel_id}/verify", operation_id="verifyNovel", include_in_schema=False)
def novel_verify_get(novel_id: str):
    try:
        return verify_novel(novel_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Novel not found")


@app.post("/novels/{novel_id}/read", operation_id="prepareNovelRead", include_in_schema=False)
def novel_read_prepare(novel_id: str):
    try:
        return prepare_novel_read(novel_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Novel not found")


@app.get("/novel-reads/{read_id}/{chunk_index}", operation_id="getNovelReadChunk")
def novel_read_chunk_get(read_id: str, chunk_index: int):
    try:
        return get_novel_read_chunk(read_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Read not found or already completed")
    except IndexError:
        raise HTTPException(status_code=404, detail="Chunk index out of range")


@app.get("/novels/{novel_id}", operation_id="getNovel", include_in_schema=False)
def novels_get(novel_id: str):
    try:
        return get_novel(novel_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Novel not found")


@app.post("/sessions", operation_id="createSession")
def sessions_create(body: SessionCreate):
    try:
        novel = get_novel(body.novel_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Novel not found")
    return create_session(novel)


@app.get("/sessions/{session_id}/preview", operation_id="getSessionPreview", include_in_schema=False)
def session_preview_get(session_id: str):
    try:
        return get_session_preview(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/sessions/{session_id}/continuation-preview", operation_id="previewContinuationSession", include_in_schema=False)
def continuation_preview_get(session_id: str):
    try:
        return build_continuation_preview(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/sessions/{session_id}/continuation/compaction", operation_id="prepareContinuationCompaction")
def continuation_compaction_prepare(session_id: str):
    try:
        return prepare_continuation_compaction(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/sessions/{session_id}/continuation/blocks/{block_index}/read", operation_id="prepareContinuationBlockRead")
def continuation_block_read_prepare(session_id: str, block_index: int, migration_id: str):
    try:
        return prepare_continuation_block_read(session_id, migration_id, block_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid continuation migration")
    except IndexError:
        raise HTTPException(status_code=404, detail="Continuation block out of range")


@app.get("/sessions/{session_id}/continuation/read/{read_id}/{chunk_index}", operation_id="getContinuationCompactionChunk")
def continuation_read_chunk_get(session_id: str, read_id: str, chunk_index: int, migration_id: str):
    try:
        return get_continuation_read_chunk(session_id, migration_id, read_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid continuation read")
    except IndexError:
        raise HTTPException(status_code=404, detail="Continuation chunk out of range")


@app.post("/sessions/{session_id}/continuation/blocks/{block_index}/commit", operation_id="commitContinuationBlock")
def continuation_block_commit(session_id: str, block_index: int, body: ContinuationBlockCommit):
    try:
        return commit_continuation_block(session_id, body.migration_id, block_index, body.summary)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid continuation migration")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/sessions/{session_id}/continuation/final/read", operation_id="prepareContinuationFinalRead")
def continuation_final_read_prepare(session_id: str, migration_id: str):
    try:
        return prepare_continuation_final_read(session_id, migration_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid continuation migration")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/sessions/{session_id}/continuation/final/commit", operation_id="commitContinuationFinal")
def continuation_final_commit(session_id: str, body: ContinuationFinalCommit):
    try:
        return commit_continuation_final(session_id, body.migration_id, body.package)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid continuation migration")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/sessions/{session_id}/continuation", operation_id="createContinuationSession")
def continuation_create(session_id: str):
    try:
        return create_continuation_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/sessions/{session_id}", operation_id="getSession", include_in_schema=False)
def sessions_get(session_id: str):
    try:
        return load_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/sessions/{session_id}/recover-current", operation_id="recoverSessionCurrent")
def session_current_recover(session_id: str):
    try:
        return recover_session_current(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "CURRENT_RECOVERY_NO_EVIDENCE":
            raise HTTPException(status_code=409, detail=("Current scene pointer is damaged, but the server could not recover enough evidence from starting state, committed turn patches, audit repairs, runtime presence or the latest saved scene header. Do not create a gameplay turn to guess the missing scene."))
        raise


@app.post("/sessions/{session_id}/rollback-last-turn", operation_id="rollbackLastTurn")
def session_last_turn_rollback(session_id: str, body: RollbackLastTurn):
    try:
        return rollback_last_turn_request(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except OperationReceiptConflict:
        raise HTTPException(status_code=409, detail="The rollback operation identity was reused with different data. Resume the session and use the current turn id.")
    except RollbackError as exc:
        code = str(exc)
        if code == "ROLLBACK_CONFIRMATION_REQUIRED":
            detail = "Explicit confirmation is required. Rollback is destructive and is allowed only for the latest saved turn."
        elif code == "ROLLBACK_EXPECTED_TURN_MISMATCH":
            detail = "The session turn_number changed. Resume the session and use its exact current turn number and current_turn_id."
        elif code in {"ROLLBACK_TURN_ID_REQUIRED", "ROLLBACK_EXPECTED_TURN_ID_MISMATCH"}:
            detail = "The target turn identity changed or is missing. Resume the session and use the exact current_turn_id; no mutation was performed."
        elif code == "ROLLBACK_LAST_TURN_NOT_FOUND":
            detail = "The expected last turn was not found in persistent turns. No mutation was performed."
        elif code.startswith("ROLLBACK_REPLAY_MISMATCH:"):
            detail = "Historical replay did not exactly reproduce the live canon, so rollback was refused and nothing was changed."
        else:
            detail = code
        raise HTTPException(status_code=409, detail=detail)


@app.get("/sessions/{session_id}/audit-snapshot", operation_id="getAuditSnapshot", include_in_schema=False)
def audit_snapshot_get(session_id: str):
    try:
        return get_audit_snapshot(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_NOT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is not currently required")
        raise


@app.get("/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}", operation_id="getAuditSnapshotChunk", include_in_schema=False)
def audit_snapshot_chunk_get(session_id: str, audit_id: str, chunk_index: int):
    try:
        return get_audit_snapshot_chunk(session_id, audit_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid or stale audit_id")
    except IndexError:
        raise HTTPException(status_code=404, detail="Audit chunk index out of range")


@app.post("/sessions/{session_id}/turn-packet", operation_id="prepareTurn")
def turn_packet_prepare(session_id: str, body: TurnPrepare):
    try:
        return prepare_turn_request(
            session_id,
            body.user_input,
            body.request_id,
            scene_archive_capable=bool(body.scene_archive_capable),
            replace_pending=bool(body.replace_pending),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        code = str(exc)
        if code == "AUDIT_REQUIRED":
            return {
                "audit_required": True,
                "required_audit": get_audit_snapshot(session_id),
                "instruction": (
                    "Complete required_audit first. Read remaining chunks with getTurnPacketChunk using audit_id as packet_id, "
                    "then submit the audit payload through commitTurn. After success call prepareTurn again with the same user_input/request_id."
                ),
            }
        if code == "TURN_REQUEST_ID_REUSED":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": code,
                    "message": "This request_id already belongs to different user_input. Use a new request_id for a new gameplay turn.",
                },
            )
        if code == "TURN_IN_PROGRESS":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": code,
                    "message": "Another user turn is already prepared but not committed. It was NOT deleted or replaced.",
                    "pending_turn": pending_turn_status(session_id),
                    "instruction": (
                        "Resume the existing packet: read only its unread_chunk_indices and commit that same packet once. "
                        "Do not call recoverSessionCurrent for a turn-packet error. If the user explicitly asks to rebuild a stuck uncommitted turn, "
                        "call prepareTurn with the same user_input/request_id and replace_pending=true; this archives only the pending packet and does not roll back the last committed turn."
                    ),
                },
            )
        raise


@app.get("/sessions/{session_id}/turn-packet/{packet_id}/{chunk_index}", operation_id="getTurnPacketChunk")
def turn_packet_chunk_get(session_id: str, packet_id: str, chunk_index: int):
    try:
        return get_turn_packet_chunk(session_id, packet_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid or stale packet_id/audit_id")
    except IndexError:
        raise HTTPException(status_code=404, detail="Chunk index out of range")


@app.post("/sessions/{session_id}/scene-archive/read", operation_id="prepareSceneArchiveRead")
def scene_archive_read_prepare(session_id: str, body: SceneArchiveRead):
    try:
        return prepare_scene_archive_read(session_id, body.scene_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Scene not found")


@app.get("/sessions/{session_id}/scene-archive/read/{read_id}/{chunk_index}", operation_id="getSceneArchiveChunk")
def scene_archive_chunk_get(session_id: str, read_id: str, chunk_index: int, scene_id: str | None = None):
    try:
        return get_scene_archive_chunk(session_id, scene_id, read_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Scene not found")
    except PermissionError:
        raise HTTPException(status_code=409, detail="Scene archive changed; restart the prepared read")
    except IndexError:
        raise HTTPException(status_code=404, detail="Scene archive chunk index out of range")





@app.post("/sessions/{session_id}/characters/{character_id}/knowledge-read", operation_id="prepareCharacterKnowledgeRead", include_in_schema=False)
def character_knowledge_read_prepare(session_id: str, character_id: str):
    try:
        return prepare_character_knowledge_read(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "TURN_PACKET_REQUIRED":
            raise HTTPException(status_code=409, detail="Prepare the current turn packet before reading character knowledge")
        raise


@app.get("/sessions/{session_id}/characters/{character_id}/knowledge-read/{read_id}/{chunk_index}", operation_id="getCharacterKnowledgeChunk", include_in_schema=False)
def character_knowledge_chunk_get(session_id: str, character_id: str, read_id: str, chunk_index: int):
    try:
        return get_character_knowledge_chunk(session_id, character_id, read_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "TURN_PACKET_REQUIRED":
            raise HTTPException(status_code=409, detail="Current turn packet is missing")
        raise
    except PermissionError:
        raise HTTPException(status_code=409, detail="Character knowledge changed or belongs to another packet; restart the knowledge read")
    except IndexError:
        raise HTTPException(status_code=404, detail="Character knowledge chunk index out of range")


@app.get("/sessions/{session_id}/knowledge-read-status", operation_id="getSceneKnowledgeReadStatus", include_in_schema=False)
def scene_knowledge_read_status_get(session_id: str):
    try:
        return scene_knowledge_read_status(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "TURN_PACKET_REQUIRED":
            raise HTTPException(status_code=409, detail="Prepare the current turn packet first")
        raise


@app.post("/sessions/{session_id}/characters/{character_id}/read", operation_id="prepareCharacterBundleRead")
def character_bundle_read_prepare(session_id: str, character_id: str):
    try:
        return prepare_character_bundle_read(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Character not found")


@app.get("/sessions/{session_id}/characters/{character_id}/read/{read_id}/{chunk_index}", operation_id="getCharacterBundleChunk")
def character_bundle_chunk_get(session_id: str, character_id: str, read_id: str, chunk_index: int):
    try:
        return get_character_bundle_chunk(session_id, character_id, read_id, chunk_index)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Character not found")
    except PermissionError:
        raise HTTPException(status_code=409, detail="Character dossier changed; restart the character read")
    except IndexError:
        raise HTTPException(status_code=404, detail="Character chunk index out of range")


@app.get("/sessions/{session_id}/characters/{character_id}", operation_id="getCharacterBundle", include_in_schema=False)
def character_bundle_get(session_id: str, character_id: str):
    try:
        return get_character_bundle(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Character not found")


@app.get("/sessions/{session_id}/characters/{character_id}/memory", operation_id="getCharacterMemory", include_in_schema=False)
def character_memory_get(session_id: str, character_id: str):
    try:
        return get_character_memory(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/sessions/{session_id}/turns", operation_id="getTurnRange", include_in_schema=False)
def turns_get(session_id: str, start_turn: int, end_turn: int):
    try:
        return {"turns": get_turn_range(session_id, start_turn, end_turn)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/sessions/{session_id}/turns", operation_id="commitTurn")
def turns_commit(session_id: str, body: CommitTurnRequest):
    if body.audit_id is not None:
        audit_body = AuditCommit(
            audit_id=body.audit_id,
            start_turn=body.start_turn,
            end_turn=body.end_turn,
            repairs=body.repairs,
            notes=body.notes,
        )
        return audit_commit(session_id, audit_body)

    turn_body = TurnCommit(
        packet_id=body.packet_id,
        user_input=body.user_input,
        scene_output=body.scene_output,
        extracted=body.extracted,
    )
    try:
        saved = commit_turn_request(session_id, turn_body.model_dump())
        commit_failure_diagnostics.clear(session_id)
        return saved
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException as exc:
        if exc.status_code == 409:
            commit_failure_diagnostics.record(
                session_id, operation="commitTurn", identity=turn_body.packet_id,
                status_code=409, detail=exc.detail,
            )
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            logging.getLogger(__name__).warning(
                "commitTurn 409 session=%s packet=%s code=%s",
                session_id, turn_body.packet_id, detail.get("code", "VALIDATION_REJECTED"),
            )
        raise
    except OperationReceiptConflict:
        commit_failure_diagnostics.record(
            session_id, operation="commitTurn", identity=turn_body.packet_id,
            status_code=409, detail="OPERATION_RECEIPT_CONFLICT",
        )
        logging.getLogger(__name__).warning("commitTurn receipt conflict session=%s packet=%s", session_id, turn_body.packet_id)
        raise HTTPException(status_code=409, detail="The packet_id was already used with a different commit payload. Prepare a fresh turn packet; no mutation was performed.")
    except RuntimeError as exc:
        code = str(exc)
        commit_failure_diagnostics.record(
            session_id, operation="commitTurn", identity=turn_body.packet_id,
            status_code=409, detail=code,
        )
        logging.getLogger(__name__).warning(
            "commitTurn runtime reject session=%s packet=%s code=%s",
            session_id, turn_body.packet_id,
            code if code.isupper() and code.isidentifier() and len(code) <= 90 else "INTERNAL_RUNTIME_ERROR",
        )
        if code == "TURN_PACKET_RUNTIME_STALE":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": code,
                    "message": "The prepared turn used an older runtime or session-data schema and was discarded before commit.",
                    "instruction": "Call prepareTurn again for the same user input. The session itself was preserved and migrated in place.",
                },
            )
        if code in {"TURN_PACKET_REQUIRED", "TURN_PACKET_INCOMPLETE"}:
            pending = pending_turn_status(session_id)
            if code == "TURN_PACKET_INCOMPLETE":
                instruction = (
                    "Do not prepare a new turn and do not call recoverSessionCurrent. "
                    "Read only pending_turn.unread_chunk_indices for this exact packet_id, then retry commitTurn once with the same payload."
                )
            else:
                instruction = (
                    "Do not invent a recovery turn. Inspect pending_turn. If it exists, resume that exact packet. "
                    "If that uncommitted packet is stuck and the user explicitly wants it rebuilt, call prepareTurn with the same user_input/request_id and replace_pending=true; "
                    "this does not touch the last committed turn. If no pending_turn exists, call prepareTurn again for the same user input/request_id."
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": code,
                    "message": "Turn packet is not ready for this commit; no canonical turn was created or deleted.",
                    "pending_turn": pending,
                    "instruction": instruction,
                },
            )
        errors = {
            "TURN_PACKET_ID_REQUIRED": "commitTurn requires the exact packet_id returned by prepareTurn",
            "RECENT_DUPLICATE_USER_INPUT": "Legacy duplicate guard rejected identical recent text. Upgrade prepareTurn to request_id semantics; no new turn was created.",
            "CAST_STORY_FUNCTION_REQUIRED": "A recurring/important story-created NPC needs character_upserts.story_function: one short director-level sentence explaining why this NPC matters to the story, not the NPC's personal goal.",
            "CAST_PERSISTENT_NPC_CARD_REQUIRED": "A named NPC remained in persistent scene/remote state from a previous turn but still has no card. Persist that recurring NPC through character_upserts, or remove a one-off extra from persistent presence before commit.",
            "SCENE_BUILDER_REVIEW_REQUIRED": "Review the complete final scene against scene_builder before retrying the same commitTurn.",
            "PERSISTENCE_REVIEW_REQUIRED": "Review durable persistence for the completed scene before retrying the same commitTurn.",
            "KNOWLEDGE_REVIEW_REQUIRED": "Review what every participating character learned or retained before retrying the same commitTurn.",
            "RELATIONSHIP_REVIEW_REQUIRED": "Review every participating NPC->POV relationship after the completed scene. Persist each real causal change through relationship_updates, then retry the same commitTurn with relationship_reviewed=true.",
            "RELATIONSHIP_REVIEW_DETAIL_REQUIRED": "Send exactly one relationship_review row for every physical or remote NPC who participated in this turn.",
            "RELATIONSHIP_REVIEW_DETAIL_INVALID": "relationship_review must contain unique known NPC character_id values and boolean changed flags.",
            "RELATIONSHIP_REVIEW_CHANGED_WITHOUT_UPDATE": "A relationship_review row marked changed=true requires a matching causal relationship_updates row.",
            "RELATIONSHIP_REVIEW_UPDATE_CONTRADICTION": "A relationship_review row marked changed=false cannot have a relationship_updates row for that NPC.",
            "RELATIONSHIP_FOOTER_ABSENT_NPC": "The visible Relationships footer may contain only NPCs physically present at scene end. Remove remote, departed or offscreen NPC rows and retry the same commitTurn.",
            "RELATIONSHIP_UPDATE_FOR_UNSEEN_NPC": "relationship_updates may target only NPCs who physically or remotely participated in this turn.",
            "RELATIONSHIP_REVIEW_REASON_REQUIRED": "Every relationship_review row on a new turn needs a concrete current-scene reason, including changed=false.",
            "RELATIONSHIP_UPDATE_REASON_REQUIRED": "A changed relationship requires a non-empty causal reason in relationship_updates.",
            "RELATIONSHIP_REVIEW_CHANGED_WITHOUT_EFFECT": "changed=true requires an actual canonical relationship effect, not an empty or no-op update.",
            "RELATIONSHIP_EXISTING_DIMENSION_DELTA_REQUIRED": "An established relationship dimension must change through a non-zero delta from the saved value.",
            "RELATIONSHIP_NEW_DIMENSION_VALUE_REQUIRED": "A new relationship dimension must include an absolute numeric value.",
            "RELATIONSHIP_DIMENSION_DUPLICATE": "Do not send the same relationship dimension more than once in one NPC update.",
            "RELATIONSHIP_UPDATE_DUPLICATE_OWNER": "Send at most one relationship_updates row per NPC in a turn.",
        }
        if code in errors:
            raise HTTPException(status_code=409, detail=errors[code])
        raise
    except Exception:
        commit_failure_diagnostics.record(
            session_id, operation="commitTurn", identity=turn_body.packet_id,
            status_code=500, detail="COMMIT_INTERNAL_ERROR",
        )
        logging.getLogger(__name__).exception(
            "commitTurn unexpected error session=%s packet=%s", session_id, turn_body.packet_id,
        )
        raise


@app.post("/sessions/{session_id}/audit", operation_id="commitAudit", include_in_schema=False)
def audit_commit(session_id: str, body: AuditCommit):
    try:
        saved = commit_audit_request(session_id, body.model_dump())
        commit_failure_diagnostics.clear(session_id)
        return saved
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except HTTPException as exc:
        if exc.status_code == 409:
            commit_failure_diagnostics.record(
                session_id, operation="commitAudit", identity=body.audit_id,
                status_code=409, detail=exc.detail,
            )
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            logging.getLogger(__name__).warning(
                "commitAudit 409 session=%s audit=%s code=%s",
                session_id, body.audit_id, detail.get("code", "VALIDATION_REJECTED"),
            )
        raise
    except OperationReceiptConflict:
        commit_failure_diagnostics.record(
            session_id, operation="commitAudit", identity=body.audit_id,
            status_code=409, detail="OPERATION_RECEIPT_CONFLICT",
        )
        logging.getLogger(__name__).warning("commitAudit receipt conflict session=%s audit=%s", session_id, body.audit_id)
        raise HTTPException(status_code=409, detail="The audit_id was already used with different audit data. Read a fresh audit snapshot; no mutation was performed.")
    except RuntimeError as exc:
        commit_failure_diagnostics.record(
            session_id, operation="commitAudit", identity=body.audit_id,
            status_code=409, detail=str(exc),
        )
        logging.getLogger(__name__).warning(
            "commitAudit runtime reject session=%s audit=%s code=%s",
            session_id, body.audit_id,
            str(exc) if str(exc).isupper() and str(exc).isidentifier() and len(str(exc)) <= 90 else "INTERNAL_RUNTIME_ERROR",
        )
        errors = {
            "AUDIT_NOT_REQUIRED": "Audit is not currently required",
            "AUDIT_PACKET_ID_REQUIRED": "The audit payload requires the exact audit_id returned in required_audit.",
            "AUDIT_PACKET_REQUIRED": "Use required_audit, read every remaining chunk through getTurnPacketChunk with audit_id as packet_id, then retry commitTurn with the audit payload.",
            "AUDIT_PACKET_INCOMPLETE": "Read every remaining audit chunk through getTurnPacketChunk before retrying commitTurn with the audit payload.",
            "AUDIT_REPAIR_TURN_REQUIRED": "Every historical audit repair must include its original turn number.",
            "AUDIT_REPAIR_TURN_OUT_OF_RANGE": "An audit repair referenced a turn outside the exact audited range.",
            "AUDIT_RELATIONSHIP_REPAIR_INVALID": "A relationship repair is inconsistent with the canonical relationships.json store.",
            "CAST_STORY_FUNCTION_REQUIRED": "A newly promoted recurring NPC needs a short story_function.",
            "SCENE_COMPACTION_REQUIRED": "Audit must include repairs.scene_compactions covering the complete 15-turn range.",
            "SCENE_COMPACTION_INVALID": "scene_compactions is malformed. Use contiguous scene ranges with one dense summary per scene.",
            "SCENE_COMPACTION_COVERAGE_INVALID": "scene_compactions must cover every audited turn exactly once, with no gaps or overlaps.",
            "SCENE_COMPACTION_SUMMARY_INVALID": "Each scene summary must be one dense factual sentence between 60 and 1200 characters.",
            "SCENE_COMPACTION_SCENE_ID_INVALID": "A supplied scene_id must refer to the one previously open scene; new scenes should omit scene_id.",
            "MEMORY_COMPACTION_INVALID": "memory_compactions is malformed.",
            "MEMORY_COMPACTION_SOURCE_REUSED": "One source memory record cannot be compacted into multiple canonical records in the same audit.",
            "MEMORY_COMPACTION_SOURCE_UNKNOWN": "A memory_compaction referenced a missing or already superseded source record.",
            "MEMORY_COMPACTION_SOURCE_OUT_OF_RANGE": "memory_compactions may only supersede records created inside this exact audit range.",
            "MEMORY_COMPACTION_CROSS_DATE": "Do not merge knowledge journal records from different dates or periods. Split by date, or omit optional memory_compactions; source facts remain intact.",
            "MACRO_CHRONOLOGY_COMPACTION_REQUIRED": "The scheduled 60-turn macro audit is incomplete. Retry the same audit_id with repairs.chronology_compactions; the next gameplay turn remains blocked until it is saved.",
            "MACRO_CHRONOLOGY_COMPACTION_INVALID": "repairs.chronology_compactions is malformed. Each row needs DD.MM.YYYY date and a 20-1800 character summary.",
            "MACRO_CHRONOLOGY_IMPORTANT_DATE_MISSING": "The macro compaction omitted a story date that contains major/anchor/critical chronology. Add a dated summary for every important date and retry the same audit_id.",
        }
        if str(exc) in errors:
            raise HTTPException(status_code=409, detail=errors[str(exc)])
        raise
    except ValueError:
        commit_failure_diagnostics.record(
            session_id, operation="commitAudit", identity=body.audit_id,
            status_code=409, detail="AUDIT_RANGE_MISMATCH",
        )
        raise HTTPException(status_code=409, detail="Audit range does not match the current turn")
    except Exception:
        commit_failure_diagnostics.record(
            session_id, operation="commitAudit", identity=body.audit_id,
            status_code=500, detail="COMMIT_INTERNAL_ERROR",
        )
        logging.getLogger(__name__).exception(
            "commitAudit unexpected error session=%s audit=%s", session_id, body.audit_id,
        )
        raise


@app.post("/sessions/{session_id}/resume", operation_id="resumeSession")
def session_resume(session_id: str):
    try:
        # Long or pending sessions need a cheap status probe, not a replay of
        # 100+ saved turns or destructive packet/schema migrations.
        result = (
            session_checkpoint.resume_checkpoint(session_id)
            if session_checkpoint.should_use_checkpoint(session_id)
            else continue_session(session_id)
        )
        diagnostic = commit_failure_diagnostics.latest(session_id)
        if diagnostic:
            result = dict(result)
            result["last_commit_rejection"] = diagnostic
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
