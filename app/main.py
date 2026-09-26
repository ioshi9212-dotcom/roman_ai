import json
import os
from threading import Thread

from fastapi import FastAPI, HTTPException

from .audit_runtime import get_audit_snapshot, get_audit_snapshot_chunk
from .character_access import get_character_bundle
from .character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read
from .context_stats import session_context_stats
from .models import AuditCommit, NovelDraftCreate, NovelDraftIntakeChunk, NovelDraftIntakeMapping, NovelDraftLaunchState, NovelDraftReconciliation, NovelDraftSection, NovelRawSave, NovelTemplate, RollbackLastTurn, SceneArchiveRead, SessionCreate, TurnCommit, TurnPrepare
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
    version="1.15.0",
    description="Persistent isolated novel sessions with bounded writer-first context, lossless chunked setup intake, living cast rotation, memory, chronology, relationships, NPC intents, persistent story threads, recovery, rollback and audits.",
)


@app.on_event("startup")
def startup_session_migration():
    if str(os.getenv("ROMAN_MIGRATE_SESSION_ID") or "").strip():
        Thread(target=run_startup_session_migration, daemon=True, name="roman-session-migration").start()


@app.get("/health", operation_id="health")
def health():
    return {"ok": True}


@app.get("/migration-status", operation_id="getMigrationStatus")
def migration_status_get():
    return read_migration_status()


@app.get("/sessions/{session_id}/context-stats", operation_id="getSessionContextStats")
def session_context_stats_get(session_id: str):
    try:
        return session_context_stats(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/runtime", operation_id="getRuntime")
def runtime_get():
    return runtime_manifest()


@app.get("/runtime/{chunk_index}", operation_id="getRuntimeChunk")
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


@app.get("/novel-drafts/{draft_id}", operation_id="getNovelDraftStatus")
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


@app.post("/novels", operation_id="saveNovel")
def novels_save(template: NovelTemplate):
    return save_novel(template.model_dump())


@app.post("/novels/raw", operation_id="saveNovelRaw")
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


@app.get("/novels/{novel_id}/verify", operation_id="verifyNovel")
def novel_verify_get(novel_id: str):
    try:
        return verify_novel(novel_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Novel not found")


@app.post("/novels/{novel_id}/read", operation_id="prepareNovelRead")
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


@app.get("/novels/{novel_id}", operation_id="getNovel")
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


@app.get("/sessions/{session_id}/preview", operation_id="getSessionPreview")
def session_preview_get(session_id: str):
    try:
        return get_session_preview(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/sessions/{session_id}", operation_id="getSession")
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


@app.get("/sessions/{session_id}/audit-snapshot", operation_id="getAuditSnapshot")
def audit_snapshot_get(session_id: str):
    try:
        return get_audit_snapshot(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_NOT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is not currently required")
        raise


@app.get("/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}", operation_id="getAuditSnapshotChunk")
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
            knowledge_review_capable=bool(body.knowledge_review_capable),
            complete_knowledge_read_capable=bool(body.complete_knowledge_read_capable),
            relationship_review_capable=bool(body.relationship_review_capable),
            strict_knowledge_capable=bool(body.strict_knowledge_capable),
            replace_pending=bool(body.replace_pending),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        code = str(exc)
        if code == "AUDIT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is required before preparing the next turn")
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
                        "Do not call recoverSessionCurrent for a turn-packet error. Set replace_pending=true only if the user explicitly abandons the saved pending turn."
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
        raise HTTPException(status_code=403, detail="Invalid or stale packet_id")
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





@app.post("/sessions/{session_id}/characters/{character_id}/knowledge-read", operation_id="prepareCharacterKnowledgeRead")
def character_knowledge_read_prepare(session_id: str, character_id: str):
    try:
        return prepare_character_knowledge_read(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "TURN_PACKET_REQUIRED":
            raise HTTPException(status_code=409, detail="Prepare the current turn packet before reading character knowledge")
        raise


@app.get("/sessions/{session_id}/characters/{character_id}/knowledge-read/{read_id}/{chunk_index}", operation_id="getCharacterKnowledgeChunk")
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


@app.get("/sessions/{session_id}/knowledge-read-status", operation_id="getSceneKnowledgeReadStatus")
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


@app.get("/sessions/{session_id}/characters/{character_id}", operation_id="getCharacterBundle")
def character_bundle_get(session_id: str, character_id: str):
    try:
        return get_character_bundle(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except KeyError:
        raise HTTPException(status_code=404, detail="Character not found")


@app.get("/sessions/{session_id}/characters/{character_id}/memory", operation_id="getCharacterMemory")
def character_memory_get(session_id: str, character_id: str):
    try:
        return get_character_memory(session_id, character_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.get("/sessions/{session_id}/turns", operation_id="getTurnRange")
def turns_get(session_id: str, start_turn: int, end_turn: int):
    try:
        return {"turns": get_turn_range(session_id, start_turn, end_turn)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@app.post("/sessions/{session_id}/turns", operation_id="commitTurn")
def turns_commit(session_id: str, body: TurnCommit):
    try:
        return commit_turn_request(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except OperationReceiptConflict:
        raise HTTPException(status_code=409, detail="The packet_id was already used with a different commit payload. Prepare a fresh turn packet; no mutation was performed.")
    except RuntimeError as exc:
        code = str(exc)
        if code in {"TURN_PACKET_REQUIRED", "TURN_PACKET_INCOMPLETE"}:
            pending = pending_turn_status(session_id)
            if code == "TURN_PACKET_INCOMPLETE":
                instruction = (
                    "Do not prepare a new turn and do not call recoverSessionCurrent. "
                    "Read only pending_turn.unread_chunk_indices for this exact packet_id, then retry commitTurn once with the same payload."
                )
            else:
                instruction = (
                    "Do not invent a recovery turn. Inspect pending_turn. If it exists, resume that exact packet; "
                    "otherwise call prepareTurn again for the same user input/request_id. recoverSessionCurrent is only for resumeSession current_recovery_required=true."
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
            "AUDIT_REQUIRED": "Audit is required before the next turn",
            "TURN_PACKET_ID_REQUIRED": "commitTurn requires the exact packet_id returned by prepareTurn",
            "RECENT_DUPLICATE_USER_INPUT": "Legacy duplicate guard rejected identical recent text. Upgrade prepareTurn to request_id semantics; no new turn was created.",
            "PERSISTENCE_REVIEW_REQUIRED": "Before commitTurn explicitly review chronology and per-character memory. extracted must include persistence_reviewed=true plus chronology, knowledge_add, experiences_add and dialogue_memory_add arrays, even when empty.",
            "RELATIONSHIP_FOOTER_REQUIRED": "The Relationships footer is missing or empty for at least one NPC physically present in the scene. Rewrite the scene footer so EVERY present NPC has an NPC->POV relationship row. If that NPC has no saved dimensions yet, initialize 1-3 natural dimensions now; do not leave the block empty.",
            "RELATIONSHIP_FOOTER_INCOMPLETE": "A present NPC has saved relationship dimensions, but the scene footer omitted or renamed one or more of them. Rewrite the footer using all saved labels from relationship_lens, preserving current values unless this scene genuinely changed them.",
            "RUNTIME_RULES_REVIEW_REQUIRED": "Read runtime_rules completely, rewrite the scene if needed, set extracted.runtime_rules_reviewed=true and retry the same commit.",
            "SCENE_BUILDER_REVIEW_REQUIRED": "Read scene_builder completely, rewrite the scene if needed, set extracted.scene_builder_reviewed=true and retry the same commit.",
            "RUNTIME_CONTRACT_VERSION_MISMATCH": "Echo runtime_contract.version from the current turn packet in extracted.runtime_contract_version and retry the same commit.",
            "SCENE_BUILDER_STRUCTURE_INVALID": "Rewrite the scene to the exact scene_builder structure and retry the same commit.",
            "SCENE_BUILDER_HEADER_INVALID": "Rewrite the five-line scene_builder header exactly and retry the same commit.",
            "SCENE_BUILDER_SCENE_LABEL_TOO_LONG": "Rewrite the scene label to at most 10 words and retry the same commit.",
            "SCENE_BUILDER_DIVIDER_INVALID": "Use exactly one scene_builder divider in the correct position and retry the same commit.",
            "SCENE_BUILDER_OPTIONS_INVALID": "Rewrite the lower block as exactly 3 actions, 3 spoken lines and 3 thoughts, with exact headings and no extra prose between sections.",
            "SCENE_BUILDER_MAIN_LENGTH_INVALID": "Rewrite only the main scene so it is non-empty and no longer than 3000 characters. Do not pad it to a minimum length; then retry the same commit.",
            "SCENE_BUILDER_FOOTER_INVALID": "Rewrite State, Relationships and turn footer in the exact scene_builder order and retry the same commit.",
            "SCENE_BUILDER_STATE_INVALID": "State must be one State: line with no more than 10 words describing only the current POV state.",
            "SCENE_BUILDER_RELATIONSHIP_FORMAT_INVALID": "Rewrite every relationship row as Name - metric number[/delta]; metric number[/delta], with no prose.",
            "SCENE_BUILDER_CONTENT_AFTER_FOOTER": "Nothing may appear after the final turn footer. Rewrite and retry the same commit.",
            "SCENE_BUILDER_TITLE_MISMATCH": "The first line must use the session's fixed novel title exactly.",
            "SCENE_BUILDER_POV_MISMATCH": "The POV name in the scene header must match the session POV exactly.",
            "SCENE_BUILDER_TURN_FOOTER_MISMATCH": "The final turn number and cycle must match the backend's current turn exactly.",
            "RUNTIME_RULE_PLAYER_SPEECH_NOT_PRESERVED": "The player's spoken text outside parentheses was lost or rewritten. Preserve those words in the main scene and retry the same commit.",
            "RUNTIME_RULE_PLAYER_SPEECH_ORDER_INVALID": "The player's spoken segments were rendered out of left-to-right order. Replay ordered_segments sequentially, keeping each parenthesized action/thought before the later spoken segment; natural NPC reactions or pauses may occur between them.",
            "CAST_STORY_FUNCTION_REQUIRED": "A recurring/important story-created NPC needs character_upserts.story_function: one short director-level sentence explaining why this NPC matters to the story, not the NPC's personal goal.",
            "CHARACTER_KNOWLEDGE_READ_REQUIRED": "Before writing/committing this scene, fully read every required scene participant via prepareCharacterKnowledgeRead and all getCharacterKnowledgeChunk chunks. Then retry the same commit.",
            "RELATIONSHIP_REVIEW_REQUIRED": "Before commit, review whether this turn changed each participating NPC's relationship to POV. Persist every causal change through relationship_updates with reason and delta for existing metrics; if nothing changed, keep relationship_updates empty. Then set extracted.relationship_reviewed=true and retry the same commit.",
        }
        if code in errors:
            raise HTTPException(status_code=409, detail=errors[code])
        raise


@app.post("/sessions/{session_id}/audit", operation_id="commitAudit")
def audit_commit(session_id: str, body: AuditCommit):
    try:
        return commit_audit_request(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except OperationReceiptConflict:
        raise HTTPException(status_code=409, detail="The audit_id was already used with different audit data. Read a fresh audit snapshot; no mutation was performed.")
    except RuntimeError as exc:
        errors = {
            "AUDIT_NOT_REQUIRED": "Audit is not currently required",
            "AUDIT_PACKET_ID_REQUIRED": "commitAudit requires the exact audit_id returned by getAuditSnapshot",
            "AUDIT_PACKET_REQUIRED": "Call getAuditSnapshot, use its exact audit_id, then read every audit snapshot chunk before commitAudit",
            "AUDIT_PACKET_INCOMPLETE": "Every audit snapshot chunk must be read before commitAudit",
            "SCENE_COMPACTION_REQUIRED": "Audit must include repairs.scene_compactions covering the complete 15-turn range.",
            "SCENE_COMPACTION_INVALID": "scene_compactions is malformed. Use contiguous scene ranges with one dense summary per scene.",
            "SCENE_COMPACTION_COVERAGE_INVALID": "scene_compactions must cover every audited turn exactly once, with no gaps or overlaps.",
            "SCENE_COMPACTION_SUMMARY_INVALID": "Each scene summary must be one dense factual sentence between 60 and 1200 characters.",
            "SCENE_COMPACTION_SCENE_ID_INVALID": "A supplied scene_id must refer to the one previously open scene; new scenes should omit scene_id.",
            "MEMORY_COMPACTION_INVALID": "memory_compactions is malformed.",
            "MEMORY_COMPACTION_SOURCE_REUSED": "One source memory record cannot be compacted into multiple canonical records in the same audit.",
            "MEMORY_COMPACTION_SOURCE_UNKNOWN": "A memory_compaction referenced a missing or already superseded source record.",
            "MEMORY_COMPACTION_SOURCE_OUT_OF_RANGE": "memory_compactions may only supersede records created inside this exact audit range.",
        }
        if str(exc) in errors:
            raise HTTPException(status_code=409, detail=errors[str(exc)])
        raise
    except ValueError:
        raise HTTPException(status_code=409, detail="Audit range does not match the current turn")


@app.post("/sessions/{session_id}/resume", operation_id="resumeSession")
def session_resume(session_id: str):
    try:
        return continue_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
