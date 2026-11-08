# Safe Audit v2 (isolated feature branch)

Branch: `fix/context-and-audit-15-60`. **Not deployed to Amvera and not merged into main.**

## Existing-session safety contract

- Default: feature **off**. Every existing session (including V5) keeps the old nonblocking audit and old writer memory format.
- Opt-in is stored on **one session** as `meta.safe_audit_v2=true` when that session is **created**. There is no migration that turns this on for existing data.
- To trial on **new test sessions only**, start a deployment of this branch with `ROMAN_SAFE_AUDIT_NEW_SESSIONS=1`. Do **not** use the production deployment/data volume for initial trials; use a separate database/volume.
- Old raw turn archives, initial canon, cards, chronology and memory are never deleted by the writer-side view. Compaction retains raw sources marked `superseded_by` and new canonical summaries.
- This branch may be merged or deployed **only after** separate testing, a backup of the production `/data/sessions`, a migration rehearsal with a copy, and explicit approval.
- Removing this branch or switching back to main leaves the production sessions untouched because this branch does not touch the deployed main runtime.

## 15-turn audit

For opted-in sessions, the 15th, 30th, 45th, ... committed turn sets `audit_required`. `prepareTurn` refuses to create turn 16/31/... before the 15-turn audit is committed, returning `AUDIT_REQUIRED` and the audit manifest. GPT reads all 16,000-character audit chunks and writes `scene_compactions` covering every audited turn exactly once. Dated plot chronology in the same 15-turn window is condensed by story date. `memory_compactions` can merge genuinely related facts or knowledge journal entries if all distinct details remain. No fabrication of summaries for unrelated facts.

## 60-turn macro

The 60th/120th/... audit contains the **entire current chronology** and **all characters' active personal knowledge** (including offscreen NPCs), not just the current cast or last 15 turns. To commit it, the model must provide `repairs.macro_knowledge_review.reviewed_character_ids` containing every id from `macro_audit_60.required_character_ids`, plus a `repairs.memory_compactions` list (possibly empty if nothing can be consolidated safely). Legacy sessions are not subject to this new requirement.

Every date-level chronology macro-summary preserves source identifiers and important original source facts. Original scenes/turns remain in the archive. Knowledge and journal summaries preserve provenance and metadata; numbers and timestamps are conservatively checked against source records before accepting a merge.

## Writer transport

For opted-in sessions only, the writer gets active, compacted facts from the full native memory file. Superseded records are hidden **only when their actual canonical replacement exists**. The underlying native personal memory and raw audit evidence still contain the sources, so audits, rollback and repairs can inspect them. Only POV and scene participants' memory enters each main packet; absent NPC dossiers are read on demand. The main packet remains chunked at 16,000 characters. Every new gameplay packet includes both complete current rule documents from GitHub main and the complete approved questionnaire.

## Limitations and rollout checklist

This protects transport and archives, but no test can fully prove that a model-generated paragraph keeps every subjective literary nuance. Inspect summary examples after 15, 60 and 120 turns. Check old-session snapshots before and after deployment, personal knowledge boundaries, tattoo/appearance and authored-reaction markers in the packet, and confirm correct behavior through an actual test session on an isolated volume. Keep branch testing separate from production.
