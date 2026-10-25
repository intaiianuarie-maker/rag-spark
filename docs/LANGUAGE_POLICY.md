# Declared and detected document language (PD-008)

A nonblank declared language is retained after trimming and lowercasing,
including declarations outside the currently supported RO/PT/EN detector
(for example `fr` and `xx`). Detection supplies the language only when no
declaration is present. No detection result silently replaces a declaration.

`DocumentMetadata.extra` retains `declared`, `detected`, `supported_declared`,
`conflict` and `language_tag`. An unsupported declaration disagreeing with a
supported detection is flagged as a conflict. Supported declarations remain
authoritative. Downstream consumers can inspect both values without losing
the submitted provenance.

This resolves the PD-008 decision in `rag-fork-solution/DECISIONS.md`.
Verification: `test_pd008_language.py` and `test_ingestion_language.py` (nine
passing tests), Ruff and metadata Mypy checks. Final review approved
2026-10-25; Kanban card c179135235120057 closed.
