from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, Dict, List

from . import storage


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
RUNTIME_VERSION = "3.3.0-scene-picker"
AUTHOR_RUNTIME_FILES = ("rules.md", "scene_builder.md")
ACTIVE_RUNTIME_FILES = {
    "rules": "rules.md",
    "scene_builder": "scene_builder.md",
}

# The author's GitHub main is the live source for new scenes, even before a
# hosting provider redeploys. No downloaded builder is written to session files.
_SCENE_BUILDER_URL = (
    "https://raw.githubusercontent.com/ioshi9212-dotcom/"
    "roman_ai/main/runtime/scene_builder.md"
)
_SCENE_BUILDER_MAX_BYTES = 250_000
_REQUEST_DOCS: ContextVar[Dict[str, str] | None] = ContextVar(
    "roman_runtime_documents_for_request", default=None
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("SCENE_BUILDER_GITHUB_UNAVAILABLE:unexpected_redirect")


@contextmanager
def runtime_read_view():
    """Freeze one fresh builder for a prepare/commit request, never across turns."""
    token = _REQUEST_DOCS.set({})
    try:
        yield
    finally:
        _REQUEST_DOCS.reset(token)


def _fetch_github_scene_builder() -> str:
    # A unique query defeats edge cache reuse after an author edits the file.
    url = _SCENE_BUILDER_URL + "?live=" + secrets.token_hex(12)
    request = Request(
        url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "Accept": "text/plain"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=6) as response:
            binary = response.read(_SCENE_BUILDER_MAX_BYTES + 1)
        if not binary or len(binary) > _SCENE_BUILDER_MAX_BYTES:
            raise ValueError("scene_builder is empty or exceeds the size limit")
        return binary.decode("utf-8-sig")
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        # Do not silently serve an older bundled builder when GitHub is down.
        raise RuntimeError("SCENE_BUILDER_GITHUB_UNAVAILABLE") from exc


def author_runtime_sources() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name in AUTHOR_RUNTIME_FILES:
        path = RUNTIME_DIR / name
        if not path.exists():
            raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
        result[name.removesuffix(".md")] = path.read_text(encoding="utf-8")
    return result


def runtime_documents() -> Dict[str, str]:
    # Read once per prepared turn, and again for the next turn. This cache
    # exists only inside runtime_read_view(), never in persisted session state.
    cached = _REQUEST_DOCS.get()
    if cached:
        return dict(cached)

    mode = os.getenv("ROMAN_SCENE_BUILDER_SOURCE", "github").strip().lower()
    if mode not in {"github", "file"}:
        raise RuntimeError("SCENE_BUILDER_SOURCE_INVALID")

    result: Dict[str, str] = {}
    for key, name in ACTIVE_RUNTIME_FILES.items():
        if key == "scene_builder" and mode == "github":
            result[key] = _fetch_github_scene_builder()
            continue
        path = RUNTIME_DIR / name
        if not path.exists():
            raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
        result[key] = path.read_text(encoding="utf-8")
    if cached is not None:
        _REQUEST_DOCS.set(dict(result))
    return result




def runtime_revision(documents: Dict[str, str] | None = None) -> str:
    docs = documents or runtime_documents()
    digest = hashlib.sha256()
    for key in sorted(docs):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(docs[key].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]

def runtime_payload() -> Dict[str, Any]:
    documents = runtime_documents()
    return {
        "runtime_version": RUNTIME_VERSION,
        "runtime_revision": runtime_revision(documents),
        "documents": {
            "rules": documents["rules"],
            "scene_builder": documents["scene_builder"],
        },
        "instruction": "Read both documents. Rules define reading and persistence; scene_builder defines character behavior, scene writing and answer format.",
    }


def runtime_chunks() -> List[str]:
    text = json.dumps(runtime_payload(), ensure_ascii=False, separators=(",", ":"))
    return [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]


def runtime_manifest() -> Dict[str, Any]:
    chunks = runtime_chunks()
    return {
        "ok": True,
        "runtime_version": RUNTIME_VERSION,
        "runtime_revision": runtime_revision(),
        "chunk_count": len(chunks),
        "total_chars": sum(len(chunk) for chunk in chunks),
        "instruction": "Call getRuntimeChunk for every chunk index from 0 to chunk_count-1.",
    }


def runtime_chunk(chunk_index: int) -> Dict[str, Any]:
    chunks = runtime_chunks()
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise IndexError("CHUNK_OUT_OF_RANGE")
    return {
        "chunk_index": chunk_index,
        "chunk_count": len(chunks),
        "content": chunks[chunk_index],
        "all_chunks_read": chunk_index == len(chunks) - 1,
    }
