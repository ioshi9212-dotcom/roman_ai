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

Every 15 turns an audit is mandatory before the next gameplay turn. The audit is also chunked and contains the exact 15-turn range plus complete persistent context.

There is no 60-turn transfer package and no copied handoff session. A session remains the same persistent object across chats.

Continuation text:

`CONTINUE SESSION: <session_id>`

The new chat calls `resumeSession` for that same id. The next `prepareTurn` reloads current state, memory, chronology, character registry, relationships, NPC intents, story threads and runtime directly from persistent storage.

## Runtime invariants

- `scene_builder` remains the scene-format authority.
- Character card/source/chronology are author truth, not automatic character knowledge.
- Personal knowledge is stored per character.
- Relationships are `NPC -> POV`, persist across absences, and use `relationship_lens` as the authoritative model.
- A relationship changed for an NPC who leaves before the visible footer can be persisted through `extracted.relationship_updates` without displaying an absent NPC.
- Durable NPC questions/plans use `npc_intent_updates`; bounded live plot events use `story_thread_updates` and persist in `state.threads`.
- The final writer packet includes mandatory narrative guardrails, including POV activity, NPC intent drive, scene momentum and story drive.
- Audit chronology/memory repairs must keep the original turn where the event or knowledge actually occurred.

## Custom GPT

Use `/openapi.json` as the Action schema and the current instructions in `gpt/custom_gpt_instructions.md`. Runtime contracts and final narrative guardrails are delivered inside the turn packets.

## ChatGPT plugin / MCP

The same service exposes Streamable HTTP MCP at `/mcp`, alongside the existing
REST Actions. The 30 operations in `openapi.yaml` are the explicit tool allowlist;
diagnostic and unbounded memory endpoints are not exported. Tools use the original
operation IDs. Path/query arguments are named parameters; the JSON request body
is passed as `body`. Both transports call the same typed handlers, validation,
transactional storage and retry logic. No session migration is needed.

After deploying this change to Amvera, connect the plugin using a root `mcp.json`:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "roman-ai": {
      "type": "streamable-http",
      "url": "https://ai-roman-yumikofv.mia0.amvera.tech/mcp"
    }
  }
}
```

This preserves the existing API's authentication model (no additional MCP login).
The transport allows the Amvera hostname and local test hosts; add a new deployment
hostname to the transport allowlist before moving it elsewhere. Existing startup
migration hooks still run. MCP sessions are stateless; novel sessions stay durable
in the existing data directory. Do not simulate failed calls or generate a scene
when the required backend call has not succeeded.

Transport tests: `python -m pytest tests/test_mcp_transport.py -q` (requires pytest).
