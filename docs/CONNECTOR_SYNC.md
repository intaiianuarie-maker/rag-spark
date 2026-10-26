# Durable connector sync

Set `SYNC_WORKER_ENABLED=true` on the worker after applying the connector
migrations to consume Sources API triggers. The default remains off. The worker
loads persisted settings through `connectors.adapters.config_loader`; it does
not require the API application in its image or fall back to environment
configurations when that import fails. Git acquisition requires the Git binary,
which is installed in the worker image.
Persisted S3 sources require the API and worker to share the operator's
`CONNECTOR_S3_SOURCES` workspace/bucket/prefix approvals; see
[workspace sources](SOURCES.md). Internal originals cannot be a source bucket.

Each source item maps `(workspace, connector, full external ID)` to a document.
The mapping stores the full ID and uses its SHA256 as the bounded index key;
long common prefixes and equal basenames cannot alias documents. Originals are
written before document/version/job rows commit. A failed post-commit enqueue
is repaired on redelivery without creating another version. When an adapter
omits MIME metadata, the publisher infers it from the filename; declared MIME
values are preserved with their charset parameter removed.

Progress and item outcomes are persisted between pages. A resumed queued job
finishes its suffix, then performs one fresh reconciliation under the same
claim before acknowledgment. That reconciliation retries earlier failed items
and confirms complete listing membership before deleting absent items. It
never relies on a best-effort enqueue of an identical job payload. Identical
completed items are not fetched or published again.

An empty source deletes previously published items. A partial downstream purge
keeps its mapping and records failure, so the next attempt can retry every
required store. Reappearing items are published again, including identical
bytes after a tombstone or failed deletion. Jobs with unresolved failures remain
unacknowledged. The worker periodically recovers expired Redis leases.

A run identity and attempt timestamp are saved before discovery. Terminal
failure markers cover discovery and publication failures, and contain canonical
codes rather than vendor exception text. Successful full reconciliation sets
finish and success timestamps. The status API returns `last_run_id`,
`last_finished_at`, and `state` (`idle`, `running`, `succeeded`, `failed`). The UI
compares a pre-trigger server snapshot with the next attempt, then polls until
that attempt finishes; older success cannot complete the new run.

Validation uses isolated PostgreSQL schemas, Redis namespaces, S3 fixture
buckets and Qdrant collections. `tests/integration/test_connector_migrations.py`
executes actual upgrades, connector downgrades, and re-upgrades. The remote
`Connector release validation` workflow provisions all required services and
rejects skipped acceptance tests. Its downstream cases replace embedding calls
with deterministic vectors; they prove storage/queue/delete wiring, not model
quality or the separate live multilingual release gate.
