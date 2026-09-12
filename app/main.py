import json

from fastapi import FastAPI, HTTPException

from .audit_runtime import get_audit_snapshot, get_audit_snapshot_chunk
from .character_access import get_character_bundle
from .character_chunk_read import get_character_bundle_chunk, prepare_character_bundle_read
from .context_stats import session_context_stats
from .models import AuditCommit, NovelDraftCreate, NovelDraftIntakeChunk, NovelDraftIntakeMapping, NovelDraftSection, NovelRawSave, NovelTemplate, RollbackLastTurn, SessionCreate, TurnCommit, TurnPrepare
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
from .runtime_access import runtime_chunk, runtime_manifest
from .session_preview import get_session_preview
from .session_recovery import recover_session_current
from .session_runtime import continue_session, prepare_turn_packet
from .operation_service import (
    OperationReceiptConflict,
    commit_audit_request,
    commit_turn_request,
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

app = FastAPI(
    title="Roman AI",
    version="1.15.0",
    description="Persistent isolated novel sessions with bounded writer-first context, lossless chunked setup intake, living cast rotation, memory, chronology, relationships, NPC intents, persistent story threads, recovery, rollback and audits.",
)


@app.get("/health", operation_id="health")
def health():
    return {"ok": True}


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
        return save_section(draft_id, body.section_name, body.section_json)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except KeyError:
        raise HTTPException(status_code=422, detail="Unknown section_name")
    except (ValueError, TypeError) as exc:
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
            raise HTTPException(status_code=422, detail="raw_text chunk exceeds 12000 characters; split it into smaller consecutive chunks.")
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
        }
        raise HTTPException(status_code=409, detail=messages.get(code, code))


@app.get("/novel-drafts/{draft_id}", operation_id="getNovelDraftStatus")
def novel_draft_status_get(draft_id: str):
    try:
        return draft_status(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")


@app.post("/novel-drafts/{draft_id}/finalize", operation_id="finalizeNovelDraft")
def novel_draft_finalize(draft_id: str):
    try:
        return finalize_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except ValueError:
        raise HTTPException(status_code=409, detail="Draft is incomplete")
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
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Draft must be finalized first")


@app.post("/novel-drafts/{draft_id}/publish", operation_id="saveDraftToLibrary")
def novel_draft_publish(draft_id: str):
    try:
        return publish_draft_to_library(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except RuntimeError:
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
        return prepare_turn_packet(session_id, body.user_input)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is required before preparing the next turn")
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
        errors = {
            "AUDIT_REQUIRED": "Audit is required before the next turn",
            "TURN_PACKET_ID_REQUIRED": "commitTurn requires the exact packet_id returned by prepareTurn",
            "TURN_PACKET_REQUIRED": "prepareTurn must be called for this exact user input and packet_id before commitTurn",
            "TURN_PACKET_INCOMPLETE": "Every turn packet chunk must be read before commitTurn",
            "PERSISTENCE_REVIEW_REQUIRED": "Before commitTurn explicitly review chronology and per-character memory. extracted must include persistence_reviewed=true plus chronology, knowledge_add, experiences_add and dialogue_memory_add arrays, even when empty.",
            "RELATIONSHIP_FOOTER_REQUIRED": "The Relationships footer is missing or empty for at least one NPC physically present in the scene. Rewrite the scene footer so EVERY present NPC has an NPC->POV relationship row. If that NPC has no saved dimensions yet, initialize 1-3 natural dimensions now; do not leave the block empty.",
            "RELATIONSHIP_FOOTER_INCOMPLETE": "A present NPC has saved relationship dimensions, but the scene footer omitted or renamed one or more of them. Rewrite the footer using all saved labels from relationship_lens, preserving current values unless this scene genuinely changed them.",
        }
        if str(exc) in errors:
            raise HTTPException(status_code=409, detail=errors[str(exc)])
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
