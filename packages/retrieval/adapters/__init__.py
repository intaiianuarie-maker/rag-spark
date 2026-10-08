"""Retrieval adapters (M3): vendor integrations behind the `retrieval.interfaces` ABCs.

Each module isolates one external system (Qdrant, Postgres FTS, infinity, ...)
and normalizes its output to our models. No vendor type crosses the adapter
boundary. License/attribution is recorded per adapter in
`rag-fork-solution/REUSE_MAP.md` + `NOTICES.md`.
"""
