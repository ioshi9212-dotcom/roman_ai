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

## Turn cycle

Every gameplay turn uses a lossless chunked turn packet and is written back to the same Railway session.

Technical audits are optional and do not block gameplay every 15 turns. Use them only when the client exposes the corresponding actions.

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
