# V1 Q1 zero-vector guard — APPROVE, scoped only

Reviewed `handoffs/CLINE1-V1-Q1-R1.md` against the actual uncommitted diff.
The shared `_embed` path rejects vectors below L2 norm 1e-9 with
`EmbeddingProviderError`, for both documents and queries. A mixed response
raises rather than returning preceding valid vectors. Existing ordering,
batching, dimension handling, and empty-input behavior remain covered.

Independent verification from the application root:

- `/root/ko-venv/bin/python3 -m pytest tests/contracts/test_infinity_embedding_contract.py -q`: **17 passed in 10.15s** (local fake HTTP server, socket access enabled).
- `/root/ko-venv/bin/ruff check packages/retrieval/adapters/infinity_embed.py tests/contracts/test_infinity_embedding_contract.py`: **All checks passed**.
- `/root/ko-venv/bin/python3 -m mypy packages/retrieval`: **9 source files clean**.
- Independent injected `httpx.MockTransport` probe: 1024 components of 1e-12
  rejected with the typed error for both query and document calls.

Reviewed SHA-256 snapshots:

| File | SHA-256 |
|---|---|
| `packages/retrieval/adapters/infinity_embed.py` | `c3538b6e46055dc02f37df21956918d6e59f795b0942999c65294d22a83d7c3e` |
| `tests/contracts/test_infinity_embedding_contract.py` | `6797245bb73008982247317b1d0a18832d6fbdf09180406361994915799d6a93` |

No blocking finding within this bounded zero/near-zero task. Non-finite values
were explicitly excluded: an independent probe confirms a JSON string `nan`
is converted to NaN and accepted for both entry points. This needs a separate
Q1 task before claiming general embedding validity.

Handoff wording corrections: use **submitted / verified**, not agent-owned
**CLOSED**; the coordinator owns acceptance. The active test interpreter is
Python 3.14, not the handoff's stated 3.12. The mixed-batch guard can append
earlier vectors to a local list before raising; those vectors never escape the
call. These wording issues do not change the verified behavior.

This approval closes only the zero-vector correction. V1 product acceptance,
live ingestion/store lifecycle, Q8/Q9, V2 G4/G5, merge, and deployment remain open.
