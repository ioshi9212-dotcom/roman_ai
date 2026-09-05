from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import storage


RUNTIME_DIR = Path(__file__).resolve().parent.parent / "runtime"
RUNTIME_VERSION = "1.9.2"
RUNTIME_FILES = (
    "rules.md",
    "scene_builder.md",
    "pov_contract.md",
    "npc_agency_contract.md",
    "relationship_contract.md",
    "presence_contract.md",
    "memory_contract.md",
    "continuity_contract.md",
)
CINEMATIC_COVERAGE_FILE = "cinematic_coverage.md"


def _runtime_compat(name: str, text: str) -> str:
    """Replace only stale transport paths while keeping story/runtime semantics intact."""
    if name == "scene_builder.md":
        text = text.replace(
            "`personal_memory` / `memory_full.characters[character_id]`",
            "`character_memory[character_id]`",
        )
    if name == "rules.md":
        text = text.replace(
            "Полные карточки всех зарегистрированных персонажей брать из `all_character_cards`, а их personal memory из `memory_full.characters[character_id]` уже прочитанного turn packet. Отдельный character bundle/memory Action для входа персонажа не требуется и не должен блокировать сцену.",
            "Полные dossiers в обычном packet есть для POV, присутствующих и реально затронутых вводом персонажей. Если зарегистрированный offscreen NPC должен войти/существенно действовать и dossier отсутствует, сначала `prepareCharacterBundleRead`, затем прочитать ВСЕ `getCharacterBundleChunk` по одному. Direct character bundle/memory Action не использовать.",
        )
        text = text.replace(
            "5. Для каждого присутствующего/входящего NPC проверить `all_character_cards[character_id]`, `memory_full.characters[character_id]`, pov_familiarity и relationship_to_pov из turn packet.\n6. Не вызывать отдельный character bundle/memory Action: пакет уже содержит полный dossier зарегистрированных персонажей.",
            "5. Для каждого присутствующего NPC проверить `character_cards`, `character_memory[character_id]`, pov_familiarity и relationship_to_pov.\n6. Для входящего offscreen NPC без dossier выполнить `prepareCharacterBundleRead`, затем прочитать все `getCharacterBundleChunk` по одному до его действий.",
        )
    return text


def runtime_documents() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for name in RUNTIME_FILES:
        path = RUNTIME_DIR / name
        if not path.exists():
            raise RuntimeError(f"RUNTIME_FILE_MISSING:{name}")
        result[name.removesuffix(".md")] = _runtime_compat(name, path.read_text(encoding="utf-8"))

    cinematic_path = RUNTIME_DIR / CINEMATIC_COVERAGE_FILE
    if not cinematic_path.exists():
        raise RuntimeError(f"RUNTIME_FILE_MISSING:{CINEMATIC_COVERAGE_FILE}")
    cinematic = cinematic_path.read_text(encoding="utf-8").strip()
    result["scene_builder"] = result["scene_builder"].rstrip() + "\n\n" + cinematic + "\n"
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
