from typing import Any, Dict

from . import novel_access, storage


RESUME_CHUNK_CHARS = novel_access.NOVEL_READ_CHUNK_CHARS


def prepare_resume_read(session_id: str) -> Dict[str, Any]:
    package = storage.build_resume_package(session_id)
    manifest = novel_access.prepare_template_read(package, "resume", session_id)
    return {
        "session_id": session_id,
        "read_id": manifest["read_id"],
        "resume_token": package["resume_token"],
        "chunk_count": manifest["chunk_count"],
        "total_chars": manifest["total_chars"],
        "instruction": (
            "Read every chunk from 0 through chunk_count-1 in order. Preferred action: getResumeChunk. "
            "Compatibility action: getNovelReadChunk works with the same read_id. Reconstruct the JSON package, "
            "then call confirmResume with resume_token only after all chunks were read."
        ),
    }


def get_resume_chunk(session_id: str, read_id: str, chunk_index: int) -> Dict[str, Any]:
    result = novel_access.get_novel_read_chunk(read_id, chunk_index)
    if result.get("source_type") != "resume" or result.get("source_id") != session_id:
        raise PermissionError("INVALID_RESUME_READ")
    return {
        "session_id": session_id,
        "read_id": read_id,
        "chunk_index": result["chunk_index"],
        "chunk_count": result["chunk_count"],
        "content": result["content"],
        "all_chunks_read": result["all_chunks_read"],
    }
