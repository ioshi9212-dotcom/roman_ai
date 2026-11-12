from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, Dict, List

from . import storage


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
RUNTIME_VERSION = "3.3.0-scene-picker"
AUTHOR_RUNTIME_FILES = ("rules.md", "scene_builder.md")
BUILDER_FILES = {
    "orig": "scene_builder.md",
    "v2": "scene_builder_v2.md",
    "v3": "scene_builder_v3.md",
}
BUILDER_LABELS = {"orig": "Orig", "v2": "V2", "v3": "V3"}
ACTIVE_RUNTIME_FILES = {
    "rules": "rules.md",
    "scene_builder": BUILDER_FILES["orig"],
    "scene_builder_v2": BUILDER_FILES["v2"],
    "scene_builder_v3": BUILDER_FILES["v3"],
}

# The author's GitHub main is the live source for new scenes, even before a
# hosting provider redeploys. Each packet carries the loaded rule documents.
_RUNTIME_GITHUB_URL = (
    "https://raw.githubusercontent.com/ioshi9212-dotcom/"
    "roman_ai/main/runtime/"
)
_RUNTIME_FILE_MAX_BYTES = 250_000
_REQUEST_DOCS: ContextVar[Dict[str, str] | None] = ContextVar(
    "roman_runtime_documents_for_request", default=None
)
_REQUEST_BUILDER: ContextVar[str] = ContextVar("roman_builder_version", default="orig")


def normalize_builder_version(version: str | None = None) -> str:
    value = str(version or "orig").strip().lower()
    if value not in BUILDER_FILES:
        raise ValueError("BUILDER_VERSION_INVALID")
    return value


def selected_builder_version() -> str:
    return _REQUEST_BUILDER.get()


def select_builder_version(version: str | None = None) -> None:
    value = normalize_builder_version(version)
    if value != selected_builder_version():
        _REQUEST_BUILDER.set(value)
        if _REQUEST_DOCS.get() is not None:
            _REQUEST_DOCS.set({})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise RuntimeError("SCENE_BUILDER_GITHUB_UNAVAILABLE:unexpected_redirect")


@contextmanager
def runtime_read_view(builder_version: str | None = None):
    """Freeze the selected live rules for one request, never across requests."""
    version = normalize_builder_version(builder_version or selected_builder_version())
    builder_token = _REQUEST_BUILDER.set(version)
    token = _REQUEST_DOCS.set({})
    try:
        yield
    finally:
        _REQUEST_DOCS.reset(token)
        _REQUEST_BUILDER.reset(builder_token)


def _fetch_github_runtime_file(key: str) -> str:
    started = perf_counter()
    succeeded = False
    # A unique query defeats edge cache reuse after an author edits the file.
    url = _RUNTIME_GITHUB_URL + ACTIVE_RUNTIME_FILES[key] + "?live=" + secrets.token_hex(12)
    request = Request(
        url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "Accept": "text/plain"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=6) as response:
            binary = response.read(_RUNTIME_FILE_MAX_BYTES + 1)
        if not binary or len(binary) > _RUNTIME_FILE_MAX_BYTES:
            raise ValueError("runtime file is empty or exceeds the size limit")
        text = binary.decode("utf-8-sig")
        succeeded = True
        return text
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        # Do not silently serve an older bundled builder when GitHub is down.
        raise RuntimeError(f"{'RULES' if key == 'rules' else 'SCENE_BUILDER'}_GITHUB_UNAVAILABLE") from exc
    finally:
        logging.getLogger("uvicorn.error").info(
            "roman_timing operation=runtime_fetch file=%s duration_ms=%.2f success=%s",
            key, (perf_counter() - started) * 1000, succeeded,
        )


def author_runtime_sources() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, name in (("rules", "rules.md"), ("scene_builder", BUILDER_FILES[selected_builder_version()])):
        path = RUNTIME_DIR / name
        if not path.exists():
            raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
        result[key] = path.read_text(encoding="utf-8")
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

    if mode == "github":
        version = selected_builder_version()
        keys = ("rules", "scene_builder" if version == "orig" else f"scene_builder_{version}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = dict(zip(("rules", "scene_builder"), pool.map(_fetch_github_runtime_file, keys)))
    else:
        result = author_runtime_sources()
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

def runtime_revision_for_builder(version: str | None = None) -> str:
    selected = normalize_builder_version(version)
    if selected == selected_builder_version():
        return runtime_revision()
    with runtime_read_view(selected):
        return runtime_revision()


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
