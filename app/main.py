import json

from fastapi import FastAPI, HTTPException

from .audit_runtime import (
    clear_audit_packet,
    get_audit_snapshot,
    get_audit_snapshot_chunk,
    require_complete_audit_read,
)
from .character_access import get_character_bundle
from .context_stats import session_context_stats
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
from .runtime_access import runtime_chunk, runtime_manifest
from .session_preview import get_session_preview
from .session_recovery import recover_session_current
from .session_runtime import commit_audit, commit_turn, continue_session, prepare_turn_packet
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


app = FastAPI(
    title="Roman AI",
    version="1.7.8",
    description="Persistent isolated novel sessions with complete chunked canon, runtime rules, character cards, memory, chronology, relationships, recovery and audits.",
)


_BATCH_MAX = 4


def _read_chunk_batch(getter, session_id: str, read_id: str, start_index: int, count: int):
    if start_index < 0:
        raise ValueError("start_index must be >= 0")
    if count < 1 or count > _BATCH_MAX:
        raise ValueError(f"count must be between 1 and {_BATCH_MAX}")

    parts = []
    first = None
    for chunk_index in range(start_index, start_index + count):
        try:
            item = getter(session_id, read_id, chunk_index)
        except IndexError:
            if not parts:
                raise
            break
        if first is None:
            first = item
        parts.append(item)
        if chunk_index + 1 >= int(item.get("chunk_count", 0)):
            break

    if not parts or first is None:
        raise IndexError(start_index)
    chunk_count = int(first.get("chunk_count", 0))
    end_index = int(parts[-1]["chunk_index"])
    return {
        "start_index": start_index,
        "end_index": end_index,
        "chunk_count": chunk_count,
        "chunks_read": [int(item["chunk_index"]) for item in parts],
        "content": "".join(str(item.get("content", "")) for item in parts),
        "all_chunks_read": bool(parts[-1].get("all_chunks_read")),
        "next_start_index": None if end_index + 1 >= chunk_count else end_index + 1,
    }


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
def novel_draft_session_create(draft_id: str, body: SessionCreate = None):
    try:
        return create_session_from_draft(draft_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Draft not found")
    except RuntimeError:
        raise HTTPException(status_code=409, detail="Draft must be finalized first")
