from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import storage


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
RUNTIME_VERSION = "1.7.3"
RUNTIME_FILES = (
    "rules.md",
    "scene_builder.md",
    "pov_contract.md",
    "npc_agency_contract.md",
    "memory_contract.md",
    "continuity_contract.md",
)


def runtime_documents() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name in RUNTIME_FILES:
        path = RUNTIME_DIR / name
        if not path.exists():
            raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
        result[name.removesuffix(".md")] = path.read_text(encoding="utf-8")
    return result


def runtime_payload() -> Dict[str, Any]:
    return {
        "runtime_version": RUNTIME_VERSION,
        "documents": runtime_documents(),
        "instruction": "Read every runtime chunk. No runtime document is summarized or truncated.",
    }


def runtime_chunks() -> List[str]:
    text = json.dumps(runtime_payload(), ensure_ascii=False, separators=(",", ":"))
    return [text[i:i + storage.MAX_PACKET_CHARS] for i in range(0, len(text), storage.MAX_PACKET_CHARS)] or ["{}"]


def runtime_manifest() -> Dict[str, Any]:
    chunks = runtime_chunks()
    return {
        "ok": True,
        "runtime_version": RUNTIME_VERSION,
        "chunk_count": len(chunks),
        "total_chars": sum(len(chunk) for chunk in chunks),
        "instruction": "Call getRuntimeChunk for every chunk index from 0 to chunk_count-1. Runtime is chunked, never shortened.",
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
