from fastapi import FastAPI, HTTPException

from .models import AuditCommit, NovelTemplate, ResumeConfirm, SessionCreate, TurnCommit
from .storage import (
    build_resume_package,
    commit_audit,
    commit_turn,
    confirm_resume,
    create_session,
    get_character_memory,
    get_novel,
    get_turn_range,
    list_novels,
    load_session,
    save_novel,
)


app = FastAPI(
    title="Roman AI",
    version="0.4.0",
    description="Novel session backend for Custom GPT. Persistent state and character memory are stored on a Railway Volume.",
)


@app.get("/health", operation_id="health")
def health():
    return {"ok": True}


@app.get("/novels", operation_id="listNovels")
def novels_list():
    return {"novels": list_novels()}


@app.post("/novels", operation_id="saveNovel")
def novels_save(template: NovelTemplate):
    return save_novel(template.model_dump())


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


@app.get("/sessions/{session_id}", operation_id="getSession")
def sessions_get(session_id: str):
    try:
        return load_session(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


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
        if str(exc) == "AUDIT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is required before the next turn")
        if str(exc) == "HANDOFF_REQUIRED":
            raise HTTPException(status_code=409, detail="Session handoff is required before the next turn")
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
        return build_resume_package(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_REQUIRED":
            raise HTTPException(status_code=409, detail="Turn 60 audit must be completed before handoff")
        if str(exc) == "HANDOFF_NOT_REQUIRED":
            raise HTTPException(status_code=409, detail="This session does not currently require handoff")
        raise


@app.post("/sessions/{session_id}/resume/confirm", operation_id="confirmResume")
def session_resume_confirm(session_id: str, body: ResumeConfirm):
    try:
        return confirm_resume(session_id, body.resume_token)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Invalid resume token")
    except RuntimeError as exc:
        if str(exc) == "AUDIT_REQUIRED":
            raise HTTPException(status_code=409, detail="Audit is required before resume can be confirmed")
        raise
