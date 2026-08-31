import json
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .character_access import get_character_bundle
from .models import AuditCommit, NovelDraftCreate, NovelDraftSection, NovelRawSave, NovelTemplate, SessionCreate, TurnCommit, TurnPrepare
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
from .session_preview import get_session_preview
from .session_runtime import commit_audit, commit_turn, continue_session, prepare_turn_packet
from .storage import (
    create_session,
    get_audit_snapshot,
    get_character_memory,
    get_novel,
    get_turn_packet_chunk,
    get_turn_range,
    list_novels,
    load_session,
    save_novel,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT_DIR / "runtime"


app = FastAPI(
    title="Roman AI",
    version="1.6.0",
    description="Persistent isolated novel sessions with compact long-range chronology and per-character memory.",
)


@app.get("/health", operation_id="health")
def health():
    return {"ok": True}


@app.get("/runtime", operation_id="getRuntime")
def runtime_get():
    def read(name: str) -> str:
        path = RUNTIME_DIR / name
        if not path.exists():
            raise HTTPException(status_code=500, detail=f"Runtime file missing: {name}")
        return path.read_text(encoding="utf-8")

    result = {
        "rules": read("rules.md"),
        "scene_builder": read("scene_builder.md"),
        "memory_contract": read("memory_contract.md"),
    }
    continuity = RUNTIME_DIR / "continuity_contract.md"
    if continuity.exists():
        result["continuity_contract"] = continuity.read_text(encoding="utf-8")
    return result


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
        raise HTTPException(status_code=422, detail=f"Invalid section_json: {exc}")


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
        raise HTTPException(status_code=409, detail="Draft must be finalized first")


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
        return commit_turn(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        errors = {
            "AUDIT_REQUIRED": "Audit is required before the next turn",
            "TURN_PACKET_REQUIRED": "prepareTurn must be called for this exact user input before commitTurn",
            "TURN_PACKET_INCOMPLETE": "Every turn packet chunk must be read before commitTurn",
            "PERSISTENCE_REVIEW_REQUIRED": "Before commitTurn explicitly review chronology and per-character memory. extracted must include persistence_reviewed=true plus chronology, knowledge_add, experiences_add and dialogue_memory_add arrays, even when empty.",
        }
        if str(exc) in errors:
            raise HTTPException(status_code=409, detail=errors[str(exc)])
        raise


@app.post("/sessions/{session_id}/audit", operation_id="commitAudit")
def audit_commit(session_id: str, body: AuditCommit):
    try:
        return commit_audit(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_NOT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is not currently required")
        raise
    except ValueError:
        raise HTTPException(status_code=409, detail="Audit range does not match the current turn")


@app.post("/sessions/{session_id}/resume", operation_id="resumeSession")
def session_resume(session_id: str):
    try:
        return continue_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
