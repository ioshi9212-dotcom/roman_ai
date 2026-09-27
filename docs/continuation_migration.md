# Continuation migration playbook

This file preserves the temporary long-session migration workflow that was removed from the everyday Custom GPT Actions surface after the current old novel was migrated.

## What stays in the backend

Do not delete the continuation backend code. The endpoints and runtime remain in:
- `app/continuation_runtime.py`
- `app/main.py`

The everyday `openapi.yaml` may omit these endpoints so gameplay actions such as rollback and scene archive fit comfortably inside the Custom GPT Actions operation limit.

## Saved migration schema

The exact Actions schema used for continuation migration on 2026-09-28 is preserved at:
- `archive/openapi_continuation_actions_2026-09-28.yaml`

The matching GPT instruction text is preserved at:
- `archive/custom_gpt_instructions_continuation_2026-09-28.md`

To migrate another oversized session later, temporarily use that saved migration schema/instruction set (or copy only the continuation operations back into the active schema), perform the migration, verify the new session, then restore the everyday gameplay schema.

## Continuation workflow

1. Call `prepareContinuationCompaction`.
2. For every block beginning at `next_block_index`: call `prepareContinuationBlockRead`, read every `getContinuationCompactionChunk`, then call `commitContinuationBlock` with a semantic summary that preserves facts and keeps character knowledge separated.
3. After all blocks: call `prepareContinuationFinalRead`, read every chunk, then call `commitContinuationFinal`.
4. The final package must globally remove repetition, reconstruct `current` from the latest exact committed turns, update/close stale threads, and must not rewrite relationship stores.
5. Only after a successful final commit call `createContinuationSession`.
6. If the final package shape is wrong, or the created continuation has a wrong current/final scene, do not repeat all blocks. Re-open the final read for the same migration, correct the final package, commit it again, then create a new continuation session.
7. Do not mutate the source session and do not replay bridge turns as new gameplay turns.

## Bridge-turn rule

A newly created continuation starts at technical `turn_number = 0`. The last exact source-session turns are copied into `handoff_tail.json` / `continuation_handoff.json` only as continuity context.

Therefore:
- a scene generated after migration is a normal new turn (1, 2, 3...) and can be rolled back normally;
- the inherited last scene from the source session is not turn 1 of the new session and cannot be deleted by `rollbackLastTurn` from the new session itself;
- rolling back new-session turn 1 returns the continuation to turn 0 while keeping the inherited bridge context intact.

## Continuation operations temporarily removed from everyday openapi.yaml

- `prepareContinuationCompaction`
- `prepareContinuationBlockRead`
- `getContinuationCompactionChunk`
- `commitContinuationBlock`
- `prepareContinuationFinalRead`
- `commitContinuationFinal`
- `createContinuationSession`
