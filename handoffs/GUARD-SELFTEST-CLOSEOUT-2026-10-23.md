# Guard selftest closeout candidate

TASK: T-IMP-007; remaining regression scope from
`../rag-fork-solution/tasks/POLICE-FIVE-TICKET-CLOSEOUT-2026-10-23.md`.

CHANGE: `scripts/model_origin_guard.sh` preserves `.py` dispatch for copied
Python negative fixtures, appends valid quoted Python assignments, and labels
Python versus bare dotenv cases accurately. Adds positive dynamic RHS and
export controls, duplicate-first/middle dotenv controls, and malformed dotenv.
Removes unused manual parser helpers and corrects the dependency description.

WHY: A rejection must exercise model validation rather than unrelated syntax
failure. PD-005 policy arrays and runtime validation behavior are unchanged.

REUSED: Existing Python AST and python-dotenv parser; no new dependencies or
third-party source copied. BUILT: Narrow fixture and selftest corrections only.

VERIFY: `bash -n scripts/model_origin_guard.sh` and `git diff --check` exit 0.
Working-tree snapshot verification command:
`python3 /tmp/super-rag-candidate-snapshot-gates.py 9536fcb` exits 0.
Evidence: `/tmp/super-rag-final-9536fcb-_7lz4bxy/results.json` and manifest.
Guard SHA256: `02e6b7a321bc216c015f1e0531607fba9a2bb43af66272bc771d432ed58b9dd2`.

- Default and canonical (`/root/ko-venv/bin` first in PATH) baseline scans exit 0.
- Both selftests exit 0, with **63** successful controls each; the earlier dev1
  claim of 71 controls was inaccurate.
- All 34 independent runtime contracts pass (runner exit 0).
- Disabling both allowlist membership branches in a disposable snapshot makes
  selftest exit 1. All six Python adapter FIRST/SECOND controls explicitly
  report expected exit 1, got 0. Evidence: `mutation-selftest.log` in that
  directory. Parent 9536fcb failed this criterion; its negative Python fixtures
  falsely passed despite disabled validation, as recorded in
  `/tmp/super-rag-final-9536fcb-vhogot99/mutation-selftest.log`.
- The eight parent-versus-fixed runtime cases pass, comparing dfa375a,
  6d1d0b0, and cbbb117 with 9536fcb. This snapshot run uses committed 9536fcb
  for those runtime comparisons, not the uncommitted selftest change. The
  extracted validator AST matches 9536fcb exactly after excluding the two
  removed, unused helper definitions. The immutable candidate must be checked
  directly using `python3 /tmp/super-rag-final-guard-gates.py <candidate-sha>`.

RISKS: No runtime policy or product feature changes. Four existing Dockerfile/
requirements edits and untracked `page-teeth-r3.tsx` are outside this candidate.
No deployment, restart, push, merge, or ticket completion is authorized by this
handoff. Final review and ticket acceptance remain with the designated owners.
The prior broad dev1 audit is not a reliable ticket-status source; the named
five-ticket closeout checkpoint governs. Feature evidence reuse requires source
identity verification, which the immutable runner checks against 2d1904f and
cbbb117. This is not a claim that the full unit suite passes.
