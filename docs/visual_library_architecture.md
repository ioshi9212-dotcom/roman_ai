# RomanAI visual library: architecture and rollout

Status: **feature branch only**. No production deployment, no session migration, and no image-generation integration.

## Findings from the existing repository

- FastAPI entrypoint: app/main.py; Custom GPT operations live in openapi.yaml and gpt/custom_gpt_instructions.md.
- Amvera's amvera.yaml mounts persistent storage at /data. app/storage.py uses DATA_DIR=/data by default.
- Published novels: /data/library/{novel_id}.json. Session-only drafts create a snapshot in /data/sessions/{session_id}/source.json and record source_novel_id in meta.json. Drafts live under /data/novel_drafts.
- Characters use character_id, locations location_id. Existing location_runtime resolves those canonical IDs.
- Turns, canon, character memories, rollback, migration and audits work in /data/sessions and must remain independent of image references.
- No binary upload/gallery/image generator bridge existed in the original main branch.
- Browser-specific rich cards or interactive form widgets are not a reliable property of the text response from a Custom GPT.

## Phase 1: persistent originals (implemented in this branch)

New isolated storage: /data/visual_library/{novel_id}/manifest.json and assets/{generated_uuid}.{jpg,png,webp}. Never embed images/base64 inside turns, canon or raw packet chunks.

Manifest metadata:
reference_id (stable on replace), novel_id, subject_type ('novel' | 'character' | 'location'), subject_id, category, notes, original_name, stored_name (internal only), media_type, size_bytes, dimensions, sha256, created_at and updated_at.

Multiple references per character or location are allowed. Original bytes are preserved, not rewritten or downsampled. Replacement writes a fresh blob and atomically switches metadata before deleting the previous blob. Delete removes manifest entry before removing asset. Crashes may leave orphaned blobs, which are not publicly enumerable and can be garbage-collected later. All file writes are protected by per-novel filesystem locks. Manifest writes use fsync and atomic replace.

IDs are validated; character/location links are checked against saved novel templates, session-only source snapshots and drafts. Files are detected and checked as JPEG/PNG/WEBP by Pillow; file size max 12 MiB; max 40 million pixels. Filename paths are generated on the server. SVG, arbitrary URLs, untrusted external fetches, video and executable content are not accepted.

### Management endpoints

All routes require X-Visual-Library-Key equal to the secret VISUAL_LIBRARY_API_KEY configured in the application environment. If not configured, every route returns 503 instead of accepting anonymous writes.

- GET /visual-library/{novel_id}/references: list/filter metadata
- POST /visual-library/{novel_id}/references: multipart binary upload plus subject_type, subject_id, category, notes
- PUT /visual-library/{novel_id}/references/{reference_id}: replace bytes while keeping reference_id, cannot silently relink to a different subject
- GET /visual-library/{novel_id}/references/{reference_id}/image: protected image bytes
- DELETE /visual-library/{novel_id}/references/{reference_id}: remove a reference

The key must be a long random secret and must NEVER be placed in GPT instructions, Git, prompts, scene text or browser localStorage. Do not publish these routes into the existing GPT Actions openapi.yaml before a separate authorization and capabilities review.

These routes become available only if this branch is intentionally deployed AND the environment secret is configured; this branch has not been deployed.

### Limits and follow-up hardening before production

- Verify Amvera's actual /data mount and available space/quotas in the project settings. A YAML declaration alone cannot prove persistence is provisioned.
- Enforce a proxy/web-server request-body limit and upload rate limiting upstream. The service currently validates images and caps the bytes it reads after multipart parsing; it is not a full ingress-body cap.
- Back up /data/visual_library independently. No backup policy is implemented.
- Consider private per-user auth/session identity, if this service becomes multi-user. Current admin-key design is single-owner.
- Consider generating safe small thumbnails separately: current GET serves the ORIGINAL protected bytes.
- Add orphan cleanup, dedup or quota enforcement if needed; not required for a small personal library.
- Storage is scoped by novel_id. If multiple unrelated session-only drafts reuse the same ID, they share references: assign unique canonical novel IDs before uploading.

## Phase 2: browser gallery and desktop uploads (not yet implemented)

Build an owner-only same-origin page or a separate desktop client that:
1. Authenticates without persisting secrets in client storage.
2. Lists cards grouped by character/location and category, previews authorized image blobs.
3. Uses the OS file picker (including multi-file selection) and explicit metadata.
4. Supports replace/delete with confirmations and communicates errors.
5. Supports browsing the library by canonical novel_id and entity IDs.

The Phase-1 API is a backend building block, not yet a complete click-through gallery. API docs/form can be exercised directly in a dev environment, but do not expose the production key in screenshots.

### Optional image upload from an existing GPT Action (not wired yet)

OpenAI's documented GPT Actions file mechanism can pass up to ten conversation uploads via the special JSON field openaiFileIdRefs, containing short-lived (five-minute) download links. A dedicated server endpoint could ingest those links after explicit user authorization, validate each downloaded image, then save to this library. This differs from this branch's existing direct multipart-upload endpoint. The links must never be trusted blindly: fetch only permitted OpenAI hosts, reject redirects/private IPs, limit file size and timeout, and validate decoded image bytes. The direct local-file picker remains the preferred management experience.

Official docs also state GPT Actions file **responses cannot be images or videos**. Therefore sending a saved reference back as openaiFileResponse does not solve in-chat image rendering or reliable image generation. An arbitrary URL returned by a custom Action is NOT a guaranteed input to ChatGPT's image tool.

References:
- https://developers.openai.com/api/docs/actions/sending-files
- https://developers.openai.com/api/reference/resources/images/methods/edit

## Phase 3: generation using the original image bytes (integration unverified)

Do NOT assume an Action response containing an image URL is automatically provided to the ChatGPT image generator. A Custom GPT calling an API is not the same as an ImageGen tool receiving image inputs.

Potential integration path: a separate, authenticated backend adapter requests images by reference IDs, reads original bytes from the private store, submits selected images plus a carefully scoped prompt to a supported image-editing/generation API, validates the result, then stores generated images under a DISTINCT location such as /data/generated_illustrations. This requires supported API access, billing, secrets, safety/consent checks and integration tests. No provider key or generating endpoint is introduced in Phase 1.

An illustration is a derivative output only: never mutate novel source profiles, character knowledge or canon to match it. The reference and canonical character/location profiles are authoritative.

## Phase 4: sparse scene illustrations and formatting (not yet implemented)

A scene may request an illustration candidate for: first significant visit, dream, Avern/veyr phenomena, visually important beat. No per-turn image requirement. The chosen scene must work as complete prose even if an image is unavailable. An illustration should be an optional attachment with provenance and no automatic canon write.

Keep the game's exact header and three actions/three speeches/three thoughts. Render richer messages/documents/calls through a progressive enhancement layer: use plain text/Markdown baseline, and opt-in structured client UI only when the actual client supports it. Message states (sent/delivered/read) come from known data, not invented statuses. Never rely on HTML/CSS behavior inside ChatGPT generated prose.

### Multi-selection UI

The gameplay backend already accepts one exact user_input in prepareTurn. A separate UI can queue selected actions, speech and thoughts as ordered segments, allow drag/reorder, include an editable custom-text field, and compose ONE literal input string. It must send ONE prepareTurn with ONE request_id and ONE corresponding commit, not issue a turn per checkbox. The normal free-text input stays fully supported.

A Custom GPT text response cannot be assumed to provide persistent interactive forms or sortable controls across every ChatGPT client. A dedicated webpage or compatible app/plugin surface is a more predictable implementation once its support is verified.

## Scene direction (separate editorial branch)

The author-freedom refactor branch has already started separating technical Rules from scene direction. Do not mix this visual-library branch with scene_builder changes. For Eastern Sector base scenes, prefer lively independent social routines and location-specific canon rather than forcing administrative events; do not make everyone uniformly pleasant. Do not attach image metadata to character knowledge or game-state updates.

## Safe rollout

1. Run isolated tests in the feature branch, review status and no unexpected changes.
2. Configure VISUAL_LIBRARY_API_KEY and confirm /data really persists in a non-production deployment.
3. Upload two distinct portraits, replace one, restart the test service and verify unchanged bytes/digests.
4. Create a new session and roll back a turn; verify all references remain intact.
5. Only then consider merging to main and deploying, with user approval. ChatGPT generation integration and in-scene UI require their own review.
