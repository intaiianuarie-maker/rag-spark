# V4 W0/W1 review — REQUEST CHANGES

Scope: v2 handoff `handoffs/CLINE2-V4-W0W1.md`, current shared working tree.
No merge authorized. Preserve parallel v1 edits. This is foundation review,
not production integration acceptance.

Independent verification: submitted four-suite selection **48 passed in 0.83s**.
Additional reproduction: `/tmp/v4_w0w1_review.py`, run with
`PYTHONPATH=packages /root/ko-venv/bin/python /tmp/v4_w0w1_review.py`.
These passing unit tests do not cover the following reproduced defects.

1. **P2 — Revision identity is mutable and unverified**
   (`packages/workflow_engine/interfaces.py:174`, compiler/executor boundary).
   `WorkflowRevision` is an ordinary mutable BaseModel: assigning revision=999
   and mutating nested node config succeeds after compilation. The compiler
   accepts `content_hash='not-a-hash'` unchanged. Execution accepts a separate
   revision without checking it matches the plan. Published content can change
   under a stable plan identity. Provide a defensible immutable snapshot contract,
   canonical hash construction/verification, and reject plan/revision mismatch
   before adapter calls. Test nested mutations as well as top-level assignment;
   merely freezing the outer Pydantic model is insufficient.

2. **P2 — Typed edges are not type checked**
   (`packages/workflow_engine/validator.py:45`). A declared `dict` output wired
   to a required `number` input validates successfully. Port names are checked,
   but no compatibility pass exists. Reject incompatible connections with stable
   codes and document `any` compatibility/default validation. Test before adapter
   execution, not only model construction. Reject duplicate/empty port names so
   the type contract cannot be ambiguous.

3. **P2 — Missing output is reported as COMPLETED**
   (`packages/workflow_engine/executor.py:298`). A valid trigger → condition →
   output definition with only a false-valued outgoing branch, executed with a
   true result, returns `completed`, `final_output=None`, output node `skipped`.
   The V4 plan explicitly says absent output/blocked join is never success.
   Define required output success and branch coverage; reject invalid topology
   or produce an honest unsuccessful run. Resolve skipped dependencies through
   descendants rather than abandoning their outgoing pending counters. Add
   nested branch/rejoin tests with active sibling inputs and terminal outputs.

4. **P2 — Executor violates its named-port adapter Protocol**
   (`packages/workflow_engine/executor.py:353`). One connected port is unwrapped
   to a scalar: an adapter implementing `input_data: dict[str, Any]` receives 42
   rather than `{'input': 42}` and fails. The fake's `Any` signature masks this.
   Always preserve named port mappings, apply declared defaults, and distinguish
   missing values from explicit null. Adapt fake behavior to the actual Protocol;
   add a strict conforming adapter test for single/multiple/defaulted ports.

Handoff examples also differ from implementation: EdgePortRef is not provided,
WorkflowDefinition requires id/workspace_id/name, and hashing is caller-supplied,
not computed by the model. Update examples/contracts with the corrected patch.

Next assignment: fix these four findings within existing W0/W1 allowed files,
add meaningful regression tests, send a scoped re-review handoff. Do not start
W2/W3, add dependencies, modify protected streams, mark DONE, or merge.
