# TURN PIPELINE

For every user turn:

1. Identify the active session_id.
2. Load the session before writing.
3. Read the current state, source novel, relevant characters, character knowledge, active threads and recent turns supplied by the session.
4. Read scene_builder.md.
5. Interpret the user's input without adding actions, thoughts or decisions for POV that the user did not provide.
6. Write the next continuous scene.
7. Extract persistent changes from both the user input and the generated scene.
8. Save the complete turn.
9. Update only persistent state that actually changed.
10. Return the scene only after commitTurn succeeds.

Rules:
- POV belongs to the user. The rest of the world belongs to the AI.
- NPCs act from their character, goals, knowledge, relationships and current circumstances. They do not need to be correct, rational, polite or convenient for POV.
- Never give a character information they did not perceive, receive or already know.
- Current session state overrides the original library snapshot when they conflict.
- Do not invent a repair for contradictory data. Treat it as a state problem to verify against saved turns.
