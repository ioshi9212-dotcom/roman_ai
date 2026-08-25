from fastapi import FastAPI, HTTPException

from .models import NovelTemplate, SessionCreate, TurnCommit
from .storage import (
    commit_turn,
    create_session,
    get_novel,
    list_novels,
    load_session,
    save_novel,
)


app = FastAPI(
    title="Roman AI",
    version="0.1.0",
    description="Novel session backend for Custom GPT. Persistent state is stored on a Railway Volume.",
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


@app.post("/sessions/{session_id}/turns", operation_id="commitTurn")
def turns_commit(session_id: str, body: TurnCommit):
    try:
        return commit_turn(session_id, body.model_dump())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
