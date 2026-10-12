# V2 G5 manifest — REQUEST CHANGES

Reviewed 2026-10-11. Both implementation agents are quota-blocked; this review
is saved for resumption without repeatedly prompting them.

1. **P2 — Merged manifest paths do not resolve and collide across datasets.**
   build_graph_manifest inventories FinQA and TAT-DQA relative to separate
   subroots, merges their files, but sets manifest.root to the common rag-eval
   root without prefixing relative paths. Independently all 1248 files fail
   `(Path(manifest.root) / file.rel_path).exists()`. Both metadata files are
   `test/metadata.jsonl`; family_of and sha256_by_path silently overwrite that
   duplicate key. Preserve common-root-relative dataset-qualified paths or
   explicit per-source roots; reject duplicates and regenerate the artifact.
   Test real builder with two temporary dataset subtrees and same filenames,
   asserting all paths resolve and each hash/family maps to its own source.

2. **P2 — Partition validator accepts nonexistent families.**
   validate_partitions([Partition('dev', {'real','invented'}),
   Partition('holdout', set())], known_families={'real'}) returns ok=True.
   Check assigned families against the known manifest set as well as coverage,
   and reject duplicate partition identities. Declare whether empty dev/holdout
   is permissible for inventory-only work versus a release-evaluable split;
   do not certify empty holdout as evaluation-ready. Add unknown-family and
   malformed-partition negatives with stable reporting.

Additional limits: family-level/source-file resolution is not gold chunk-ID
validation; keep that distinction explicit until authored ingestion mappings
exist. Per-file TAT-DQA families are provisional inventory grouping, not proven
source-family leakage prevention. Notes say parquet corpora are hashed here
but builder only processes the two finance subroots; correct coverage claims.

No G5 quality acceptance, protected G4 wiring, merge or deployment. Preserve
all work. Resume these corrections once an authorized model is available.
