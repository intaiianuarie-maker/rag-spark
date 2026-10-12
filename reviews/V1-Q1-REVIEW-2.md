# V1 Q1 finite-embeddings guard — APPROVE, scoped only

Reviewed `handoffs/CLINE1-V1-Q1-FINITE-R1.md` against the actual uncommitted diff.
The adapter properly wraps component conversion in narrow exception handling (`TypeError`, `ValueError`, `OverflowError`) and raises `EmbeddingProviderError` with chained causes.
Component finiteness (`math.isfinite`) is strictly checked before any dimension adaptation.
The zero-vector norm check uses `math.hypot(*vector)` which is robust against arithmetic overflow for large finite components.
Whole-call failure is preserved across single and batched embedding requests.

Independent verification from the application root:

- `/root/ko-venv/bin/python3 -m py_compile packages/retrieval/adapters/infinity_embed.py tests/contracts/test_infinity_embedding_contract.py`: **OK**.
- `/root/ko-venv/bin/python3 -m pytest tests/contracts/test_infinity_embedding_contract.py -q`: **29 passed in 15.54s** (all 8 invalid-component modes, truncated tail, near-zero, large finite, and baseline tests pass).
- `/root/ko-venv/bin/ruff check packages/retrieval/adapters/infinity_embed.py tests/contracts/test_infinity_embedding_contract.py`: **All checks passed**.
- `/root/ko-venv/bin/python3 -m mypy packages/retrieval`: **9 source files clean**.

Reviewed SHA-256 snapshots:

| File | SHA-256 |
|---|---|
| `packages/retrieval/adapters/infinity_embed.py` | `888236fe74c9fbae9965ec5acdb33bdbf3ff027a70e9344fc324d064c339a79f` |
| `tests/contracts/test_infinity_embedding_contract.py` | `fec774eb9f7ba7d0a0a1cd7e9ec3439fa948225f31a25c3fe22adf5f15b7313f` |

Scoped approval granted for V1-Q1-FINITE-EMBEDDINGS.
V1 product acceptance, live ingestion/store lifecycle, Q8/Q9 multilingual evaluation, V2 G4/G5, merge, and deployment remain open.
