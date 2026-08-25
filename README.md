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
- current state;
- chronology;
- full raw turn archive;
- audits;
- temporary six-turn handoff tail at transfer points.

## Turn cycle

Every turn is written to Railway.
Every 15 turns an audit is mandatory before the next turn.
Every 60 turns the session is locked for chat handoff after the audit.

Transfer text:

`CONTINUE SESSION: <session_id>`

The new chat calls `resumeSession`, receives the exact source/state/chronology plus turns 55-60 (or the corresponding final six turns), then calls `confirmResume`.
Only after confirmation is the temporary handoff tail deleted. The permanent raw turn archive is never deleted.

## Custom GPT

Use `/openapi.json` as the Action schema and copy the rules from `runtime/custom_gpt.md` into the Custom GPT instructions together with the scene rules.
