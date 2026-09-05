Temporary read-only production diagnostics for measuring session context size.

Runtime entrypoint for the temporary deployment:

uvicorn app.diagnostic_main:app --host 0.0.0.0 --port $PORT

The diagnostic route only reads existing session files and returns counts/sizes. It does not call commitTurn, commitAudit, recovery, or any storage write helper.
