# V2 preparation re-review — REQUEST CHANGES

Independent factory/eval tests: 21 passed in 0.68s. Oracle diagnostic rename,
UNMEASURED answer success, loaded workspace handling and G4 hydration contract
are improved. No product G4/G5 acceptance follows from these unit checks.

**P2 — Fake runtime can still be mislabeled REAL.**
run_paired_eval now accepts runtime_mode='real' and copies it straight into
PairedReport although it always constructs FakeVectorStore,
FakeEmbeddingProvider/FakeLexicalStore. summary_lines prints REAL runtime for
any value other than 'fake'. The handoff claim that runtime_mode is set to fake
unconditionally is incorrect. Remove this argument from the fake runner or
reject non-fake values; derive disclosure from executed adapters, not caller
assertion. Add regression proving attempts to request real cannot produce a
REAL-labelled fake report. Keep dataset origin separate.

Also correct current handoff verification totals (124 vs 131 elsewhere); report
the exact selected commands and counts for a stable artifact. Full production
quality remains unmeasured. No merge, deployment or protected integration.
