# MEMORY CONTRACT

The session memory is not one list. Keep three separate layers for every NPC.

## 1. knowledge
Facts the character currently believes or knows.
Each item should include:
- fact_id
- content
- source
- learned_turn
- confidence: certain | likely | doubtful

## 2. experiences
Events the character personally witnessed, heard, did or directly took part in.
Each item should include:
- event_id
- turn
- summary
- role: saw | heard | did | received | participated

## 3. dialogue_memory
Tracks topics already discussed with this character.
Each record should include:
- topic_id
- turn
- asked_by
- asked_to
- question
- answer
- status: answered | partial | refused | lied | unresolved

Rules:
- A character must not behave as if an experienced event never happened.
- A character must not ask the same answered question again by accident.
- Re-asking is allowed only when there is a reason such as doubt, checking consistency, changed circumstances, an earlier partial/refused answer, deliberate pressure, or believable memory loss established in canon.
- A lie is remembered as what the listener was told, not automatically as world truth.
- World truth, POV knowledge and each NPC's knowledge are separate.
- Do not delete old memories merely because they are not currently relevant.
- Only load relevant memories into the scene context; the full memory remains stored on Railway.
