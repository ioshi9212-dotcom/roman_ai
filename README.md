# roman_ai

Backend for a persistent interactive novel generator using Railway Volume + Custom GPT Actions.

## Railway

Deploy this repository as a Railway service.

Attach a Railway Volume mounted at:

`/data`

The app uses `DATA_DIR=/data` by default.

Start command is defined in `railway.json`.

Health check:

`GET /health`

FastAPI OpenAPI schema for Custom GPT Actions:

`https://YOUR-RAILWAY-DOMAIN/openapi.json`

## Persistent data

Library novels:

`/data/library`

Sessions:

`/data/sessions/<session_id>`

Each session keeps:
- immutable source novel snapshot;
- live character registry;
- current state and presence;
- persistent directional NPC -> POV relationships;
- per-character personal memory;
- persistent NPC intents;
- persistent active story threads;
- chronology;
- full raw turn archive;
- audits.

## Generator state documents

The repository now has exactly ten JSON templates in `state/`, copied verbatim
from `romance-novella-generator-api` at `4379d8d` (14 October 2026). The former
`state_templates/` folder is replaced, not kept as a second template set.

| Template | Live V5 session file |
|---|---|
| `сессия.json` | `сессия.json` |
| `анкета.json` | `анкета.json` |
| `персонажи.json` | `персонажи.json` |
| `state.json` | `state.json` |
| `отношения.json` | `отношения.json` |
| `локации.json` | `локации.json` |
| `скрытый_лор_и_крючки.json` | `скрытый_лор_и_крючки.json` |
| `хронология.json` | `хронология.json` |
| `карточка_персонажа.json` | `карточки/<id>.json`, including POV |
| `память_персонажа.json` | `память/<id>.json`, including POV |

The JSON template count is ten. The number of actual personal files depends on
the cast. RAW/source snapshots, exact turn archives, operation receipts and
rollback records remain technical storage, not additional author templates.

New V5/profile sessions use these files immediately. Existing V5 sessions move
once when loaded, preserving their ID, original RAW, archive, original memory
record IDs/turns and relationships. The move is transactional; old aggregate
files are removed only after the new files are committed. Pre-V5 stories remain
supported through their original format.

`prepareTurn` includes the entire dated `хронология.json` as `chronology`, and
the complete dated personal memories of physical/remote participants as
`character_memory[id].знания` and `личные_события`. It does not drop the oldest
knowledge or select only recent chronology entries. Offscreen character memory
is loaded through the existing character-bundle actions. Missing chronology or
participant memory returns `409 SESSION_DOCUMENT_REQUIRED`; a scene is never
prepared with an invented empty replacement.

Chronology is director-only. Personal facts retain their original wording,
including attributed claims, uncertainty and lies; another character's memory
does not become shared knowledge. `_runtime` preserves technical provenance for
audit/replay/rollback and is removed from writer-facing documents.

The existing Actions and `extracted` update fields remain compatible. Old
English file names are internal views, not duplicate saved memories or
chronologies. Numerical relationship behavior remains the current Roman AI
contract, including zero-axis removal and causal deltas. A request-scoped read
view avoids reopening the same native document for each compatibility helper.

Use the updated `gpt/custom_gpt_instructions.md` in the GPT configuration; the
deployed runtime Rules explain the new packet paths. Scene Builder is unchanged.

## Turn cycle

Every gameplay turn uses a lossless chunked turn packet and is written back to the same Railway session.

Technical audits are optional and do not block gameplay every 15 turns. Use them only when the client exposes the corresponding actions.

The V5 writer context is built directly, without legacy policy/guard objects,
duplicate dossiers or successive context-rewrite passes. Native preparation
loads personal memory only for POV and the actual participants. Archive parsing
and decoded compatibility views are cached for one request; acquiring a new
outer session lock refreshes views that another worker may have changed.

Ordinary commits append one exact JSONL record in the same transaction as the
state, knowledge, relationships, receipt and rollback snapshot. A prepared
transaction records the original archive length: interrupted append recovery
truncates to that length and restores the other documents. Retrying a successful
commit still replays its receipt instead of appending twice.

The turn pipeline returns `audit_blocking=false`; checkpoints only signal that maintenance is
available. Prepare, commit and resume can continue with an outstanding audit.
An audit packet is built only when explicitly requested. Older data formats
remain compatible without a mandatory checkpoint pause. Review flags remain accepted input
but no longer block commits; packet completeness and real persistence/knowledge
checks remain enforced. Historical knowledge deduplication runs once in schema
upgrade 2→3 rather than before every scene. Rules are shorter; Scene Builder and
the ten author templates are unchanged.

There is no 60-turn transfer package and no copied handoff session. A session remains the same persistent object across chats.

Continuation text:

`CONTINUE SESSION: <session_id>`

The new chat calls `resumeSession` for that same id. The next `prepareTurn` reloads current state, memory, chronology, character registry, relationships, NPC intents, story threads and runtime directly from persistent storage.

## Runtime invariants

- `scene_builder` defines character behavior, scene writing and format; `runtime_rules` defines reading and persistence.
- Character card/source/chronology are author truth, not automatic character knowledge.
- Personal knowledge is stored per character.
- Relationships are `NPC -> POV`, persist across absences, and use `relationship_lens` as the authoritative model.
- A relationship changed for an NPC who leaves before the visible footer can be persisted through `extracted.relationship_updates` without displaying an absent NPC.
- Durable NPC questions/plans use `npc_intent_updates`; bounded live plot events use `story_thread_updates` and persist in `state.threads`.
- The writer follows the active `scene_builder` and `runtime_rules` from the turn packet.
- Audit chronology/memory repairs must keep the original turn where the event or knowledge actually occurred.

## Custom GPT

Use `/openapi.json` as the Action schema and the current instructions in `gpt/custom_gpt_instructions.md`. Active runtime documents are delivered inside the turn packets.
