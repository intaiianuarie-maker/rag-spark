# Workspace sources

`/v1/workspaces/{workspace_id}/sources` stores connector configurations for
`local_upload`, `web`, `s3`, and `git`. Readers can list configurations and
inspect `/{connector_id}/status`; workspace managers can create, patch, delete,
and trigger `POST /{connector_id}/sync`. IDs are URL-safe path segments.
Foreign-workspace IDs do not resolve. Trigger acceptance means queued work,
not completed indexing.

The API and worker share `connectors.config_validation.validate_settings` and
`connectors.adapters.config_loader.load_persisted_sources`. The loader queries
only the requested workspace and validates persisted settings again on every
load. It needs no API application in the worker image.

Workspace-owned local folder and Git checkout paths must resolve beneath a
root in operator-controlled `CONNECTOR_LOCAL_ROOTS` (comma separated). Missing
roots fail closed. Git `file://` paths are decoded before containment checks.
Operator-owned environment configurations keep their separate admission path.
Web URLs require plain HTTP(S) origins. Git remote URLs use the existing bounded
Git adapter and refuse embedded passwords/HTTP credentials. Validation errors
omit rejected input, including chained Pydantic tracebacks.

Per-source `credential_ref` and S3 `endpoint_ref` values are currently rejected
because no resolver is installed. Operators configure S3 credentials/endpoints
through the existing environment and Git authentication through its environment
or credential helper. Raw credentials do not belong in saved source settings.
Optional settings with null values are omitted, so a milliseconds-only web
politeness delay is not shadowed by a null seconds value.

Migration 0015 creates the configuration table. Runtime sync and the Sources UI
have separate release gates; CRUD availability alone does not prove successful
background ingestion, deletion propagation, or browser polling.
