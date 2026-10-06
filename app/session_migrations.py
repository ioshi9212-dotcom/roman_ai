from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict

from . import personal_memory_transport, relationship_file_runtime, storage
from .transactional_storage import session_transaction


CURRENT_DATA_SCHEMA_VERSION = 2
LEGACY_RELATIONSHIP_KEYS = (
    "relationships",
    "relationship_documents",
    "relationship_schemas",
    "npc_relationships",
)


def _migrate_v0_to_v1(root: Path) -> None:
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    state = storage._read_json(root / "state.json", {})
    pov = state.get("pov") if isinstance(state.get("pov"), dict) else {}
    pov_id = str(pov.get("character_id") or storage._find_pov_id(source, cards) or "")

    relationship_file_runtime.load(
        root,
        cards=cards,
        state=state,
        pov_id=pov_id,
    )

    state = storage._read_json(root / "state.json", state)
    cleaned = dict(state) if isinstance(state, dict) else {}
    for key in LEGACY_RELATIONSHIP_KEYS:
        cleaned.pop(key, None)
    if cleaned != state:
        storage._write_json(root / "state.json", cleaned)


def _migrate_v1_to_v2(root: Path) -> None:
    source = storage._read_json(root / "source.json", {})
    cards = storage._load_cards(root, source)
    memory = storage._normalise_memory(storage._read_json(root / "memory.json", {}))
    characters = memory.get("characters") if isinstance(memory.get("characters"), dict) else {}
    changed = False

    for owner_id, bucket in characters.items():
        if not isinstance(bucket, dict):
            continue
        journal = bucket.get("knowledge_journal")
        if not isinstance(journal, list):
            continue
        for row in journal:
            if not isinstance(row, dict):
                continue
            old_text = str(row.get("text") or "")
            new_text = personal_memory_transport.neutralize_generated_remote_journal(
                old_text,
                owner_id=str(owner_id),
                cards=cards,
            )
            if new_text != old_text:
                row["text"] = new_text
                changed = True

    if changed:
        storage._write_json(root / "memory.json", memory)


_MIGRATIONS: Dict[int, Callable[[Path], None]] = {
    0: _migrate_v0_to_v1,
    1: _migrate_v1_to_v2,
}


def packet_is_current(packet: Any) -> bool:
    if not isinstance(packet, dict) or not packet.get("packet_id"):
        return False
    from . import runtime_access
    from .turn_pipeline import PIPELINE_VERSION

    return (
        int(packet.get("data_schema_version", 0) or 0) == CURRENT_DATA_SCHEMA_VERSION
        and str(packet.get("runtime_revision") or "") == runtime_access.runtime_revision()
        and int(packet.get("turn_pipeline_version", 0) or 0) == PIPELINE_VERSION
    )


def invalidate_stale_turn_packet(root: Path, *, reason: str = "runtime_or_schema_changed") -> bool:
    packet = storage._read_json(root / "turn_packet.json", {})
    if not isinstance(packet, dict) or not packet.get("packet_id") or packet_is_current(packet):
        return False

    from . import runtime_access
    from .turn_pipeline import PIPELINE_VERSION

    path = root / "abandoned_turn_packets.json"
    rows = storage._read_json(path, [])
    rows = rows if isinstance(rows, list) else []
    rows.append({
        "packet_id": str(packet.get("packet_id") or ""),
        "prepared_for_turn": int(packet.get("prepared_for_turn", 0) or 0),
        "request_id": str(packet.get("request_id") or "") or None,
        "user_input": str(packet.get("user_input") or ""),
        "reason": reason,
        "old_runtime_revision": packet.get("runtime_revision"),
        "new_runtime_revision": runtime_access.runtime_revision(),
        "old_data_schema_version": int(packet.get("data_schema_version", 0) or 0),
        "new_data_schema_version": CURRENT_DATA_SCHEMA_VERSION,
        "old_turn_pipeline_version": int(packet.get("turn_pipeline_version", 0) or 0),
        "new_turn_pipeline_version": PIPELINE_VERSION,
        "abandoned_at": datetime.now(timezone.utc).isoformat(),
    })
    storage._write_json(path, rows[-20:])
    (root / "turn_packet.json").unlink(missing_ok=True)
    return True


def ensure_current_session_data(
    session_id: str,
    *,
    invalidate_pending: bool = False,
) -> Dict[str, Any]:
    root = storage.SESSIONS_DIR / session_id
    if not root.exists():
        raise FileNotFoundError(session_id)

    migrated_from = None
    with session_transaction(root):
        meta = storage._read_json(root / "meta.json", {})
        version = int(meta.get("data_schema_version", 0) or 0)
        if version > CURRENT_DATA_SCHEMA_VERSION:
            raise RuntimeError("SESSION_DATA_SCHEMA_NEWER_THAN_RUNTIME")

        while version < CURRENT_DATA_SCHEMA_VERSION:
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"SESSION_DATA_MIGRATION_MISSING:{version}")
            if migrated_from is None:
                migrated_from = version
            migration(root)
            version += 1
            meta = storage._read_json(root / "meta.json", meta)
            meta["data_schema_version"] = version
            meta["data_schema_migrated_at"] = datetime.now(timezone.utc).isoformat()
            storage._write_json(root / "meta.json", meta)

        invalidated = invalidate_stale_turn_packet(root) if invalidate_pending else False

    return {
        "data_schema_version": CURRENT_DATA_SCHEMA_VERSION,
        "migrated": migrated_from is not None,
        "migrated_from": migrated_from,
        "pending_packet_invalidated": invalidated,
    }
