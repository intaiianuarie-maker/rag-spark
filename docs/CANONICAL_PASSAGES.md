# Canonical citation passages

`GET /v1/workspaces/{workspace_id}/chunks/{chunk_id}` returns the stored passage
behind an authenticated user's citation. The response contains `chunk_id`,
`document_id`, `workspace_id`, `filename`, `page`, `section`, `content`, and
optional stored `span`/`bbox` coordinates.

The API requires document-read permission for the requested workspace before
querying. Its SQL query constrains both chunk and document workspace IDs and
requires the document's current version, a live document and an unsuperseded
version. Missing, foreign, deleted, stale and unreadable passages share a 404
response. Anonymous requests receive 401; workspace denial receives 403;
database failure receives a generic 503.

The web `/api/passage` route forwards the caller's access cookie to this
endpoint and normalizes the canonical response for the source panel. Live
release acceptance checks the exact chunk/document identity, stored text and
visible **Canonical (live)** badge. Answer-embedded fallback text cannot satisfy
that gate.
