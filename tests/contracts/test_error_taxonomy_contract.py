"""T-V1-025 cross-contract: ONE shared error taxonomy, end to end.

Five locks, all service-free (no DB, no compose, no network):

1. **Registry invariants** — every ``ReasonCode`` is registered, canonical
   UPPER_SNAKE_CASE, with a safe message and a coherent HTTP status
   (``http_status=None`` only for answer-quality reasons + disclosures).
2. **TS mirrors backend** — the ``AnswerStatus`` / ``JobStatus`` /
   ``AnswerReasonCode`` / ``Dependency`` / ``ServiceMode`` /
   ``DependencyRequirement`` unions in ``apps/web/src/app/ask/contracts.ts``
   equal the Python enums/registry exactly (no parallel TS taxonomy).
3. **No parallel graph taxonomy** — ``packages/graph`` raises no reason-code
   literal of its own; every code it could emit resolves through the
   registry.
4. **Every emitted code is registered** — every ``reason_code`` literal in the
   answer engine, every ``IngestPipelineError("…")`` literal in the ingest
   pipeline and every typed ``IngestionError.code`` in ``ingestion/normalize``
   resolves through the registry, and the worker persists through
   ``normalize_code`` (identity on today's wire values ⇒ no behavior change).
5. **Required vs optional deps per enabled mode** — every dependency in
   ``/ready``'s probe set is classified for every mode; the answer path's
   mandatory legs map to REQUIRED deps with a registered reason code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.errors import (
    ANSWER_REASON_CODES,
    ANSWER_STATUS_REASON_CODES,
    DEPENDENCY_MATRIX,
    DISCLOSURE_CODES,
    ERROR_REGISTRY,
    JOB_STATUS_REASON_CODES,
    AnswerStatus,
    Dependency,
    DependencyRequirement,
    JobStatus,
    ReasonCode,
    ServiceMode,
    canonical_code,
    classify_dependency,
    error_detail,
    normalize_code,
    required_dependencies,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- 1. registry invariants ------------------------------------------------


def test_every_reason_code_is_registered() -> None:
    missing = [code.value for code in ReasonCode if code not in ERROR_REGISTRY]
    assert missing == [], f"unregistered reason codes: {missing}"


def test_every_registered_entry_is_coherent() -> None:
    for code, spec in ERROR_REGISTRY.items():
        assert code.value == code.value.upper(), f"{code} not UPPER_SNAKE_CASE"
        assert code.value.replace("_", "").isalnum(), f"{code} not UPPER_SNAKE_CASE"
        assert spec.category, f"{code} has no category"
        assert spec.message and spec.message[-1] == ".", f"{code}: unsafe/odd message"
        # "Safe" message lint (AGENTS safe-user-reason): no internals leaked.
        lowered = spec.message.lower()
        for banned in ("http://", "https://", "localhost", "127.0.0.1", "traceback", "select "):
            assert banned not in lowered, f"{code} message leaks internals"
        # http_status is None iff answer-quality reason or disclosure code.
        if spec.http_status is None:
            assert (
                spec.category.value == "answer" or code in DISCLOSURE_CODES
            ), f"{code}: None status outside answer/disclosure"
        else:
            assert 400 <= spec.http_status <= 599, f"{code}: odd HTTP status"


def test_wire_values_are_unique_and_resolve_round_trip() -> None:
    wires = [spec.wire_value for spec in ERROR_REGISTRY.values()]
    assert len(wires) == len(set(wires)), "duplicate wire values in the registry"
    for code, spec in ERROR_REGISTRY.items():
        assert canonical_code(code.value) is code
        assert canonical_code(str(spec.wire_value)) is code
        assert normalize_code(code.value) == spec.wire_value


def test_answer_status_and_reason_code_maps_are_complete() -> None:
    for status in AnswerStatus:
        assert status in ANSWER_STATUS_REASON_CODES, f"{status} has no reason-code set"
        for code in ANSWER_STATUS_REASON_CODES[status]:
            assert code in ERROR_REGISTRY, f"{status}: unregistered {code}"
    assert ANSWER_STATUS_REASON_CODES[AnswerStatus.ANSWERED] == ()
    for status in JobStatus:
        assert status in JOB_STATUS_REASON_CODES, f"{status} has no reason-code set"
        for code in JOB_STATUS_REASON_CODES[status]:
            assert code in ERROR_REGISTRY, f"{status}: unregistered {code}"


# --- 2. TS mirrors backend -------------------------------------------------


def _ts_union(source: str, type_name: str) -> set[str]:
    match = re.search(rf"export type {type_name} =\s*(.*?);", source, re.DOTALL)
    assert match, f"export type {type_name} union missing from contracts.ts"
    return set(re.findall(r'"([A-Za-z_]+)"', match.group(1)))


def _contracts_source() -> str:
    return (REPO_ROOT / "apps" / "web" / "src" / "app" / "ask" / "contracts.ts").read_text(
        encoding="utf-8"
    )


def test_ts_enums_mirror_the_python_taxonomy() -> None:
    source = _contracts_source()
    assert _ts_union(source, "AnswerStatus") == {s.value for s in AnswerStatus}
    assert _ts_union(source, "JobStatus") == {s.value for s in JobStatus}
    assert _ts_union(source, "AnswerReasonCode") == {c.value for c in ANSWER_REASON_CODES}
    assert _ts_union(source, "Dependency") == {d.value for d in Dependency}
    assert _ts_union(source, "ServiceMode") == {m.value for m in ServiceMode}
    assert _ts_union(source, "DependencyRequirement") == {
        r.value for r in DependencyRequirement
    }


def test_schemas_reexport_the_shared_enums_not_local_copies() -> None:
    from answer.models import AnswerStatus as ApiAnswerStatus
    from ingestion.persistence import JobStatus as WorkerJobStatus

    assert ApiAnswerStatus is AnswerStatus, "answer.models defines a parallel AnswerStatus"
    assert WorkerJobStatus is JobStatus, "ingestion.persistence defines a parallel JobStatus"


# --- 3. no parallel graph taxonomy -----------------------------------------


def test_graph_layer_has_no_parallel_taxonomy() -> None:
    graph_dir = REPO_ROOT / "packages" / "graph"
    offending: list[str] = []
    for path in graph_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        # A parallel taxonomy would declare its own reason-code enum or a
        # local registry of codes.
        if re.search(r"class\s+\w*(Reason|Error)Code\w*\s*\(", source):
            offending.append(f"{path.name}: local reason-code class")
        if re.search(r"reason_code\s*=\s*\"[A-Z_]{4,}\"", source):
            offending.append(f"{path.name}: raw reason-code literal")
    assert offending == [], f"parallel graph taxonomy detected: {offending}"


# --- 4. every emitted code is registered -----------------------------------


def test_answer_engine_literals_resolve_through_the_registry() -> None:
    source = (REPO_ROOT / "packages" / "answer" / "engine.py").read_text(encoding="utf-8")
    literals = set(re.findall(r'reason_code="([A-Z_]+)"', source))
    assert literals, "expected reason_code literals in answer engine"
    for raw in literals:
        assert canonical_code(raw) is not None, f"{raw} not in the taxonomy"
    # Every engine-emitted code is declared as an ANSWER-category reason.
    assert literals == {code.value for code in ANSWER_REASON_CODES} - {"DEGRADED_RESULT"}


def test_worker_pipeline_literals_resolve_and_wire_is_unchanged() -> None:
    source = (REPO_ROOT / "apps" / "worker" / "jobs" / "ingest_pipeline.py").read_text(
        encoding="utf-8"
    )
    literals = set(re.findall(r"IngestPipelineError\(\s*\n?\s*\"([A-Za-z_]+)\"", source))
    assert literals, "expected IngestPipelineError code literals in ingest_pipeline.py"
    for raw in literals:
        resolved = canonical_code(raw)
        assert resolved is not None, f"{raw} not in the taxonomy"
        # Identity on the current wire: this ticket changes no stored value.
        assert normalize_code(raw) == raw, f"{raw} would move on the wire"


def test_typed_ingestion_errors_resolve_through_the_registry() -> None:
    source = (REPO_ROOT / "packages" / "ingestion" / "normalize.py").read_text(
        encoding="utf-8"
    )
    typed = set(re.findall(r'code = "([a-z_]+)"', source))
    assert typed, "expected typed IngestionError codes in normalize.py"
    for raw in typed:
        assert canonical_code(raw) is not None, f"{raw} not in the taxonomy"


def test_worker_and_api_persist_through_the_registry() -> None:
    worker = (REPO_ROOT / "apps" / "worker" / "jobs" / "ingest_worker.py").read_text(
        encoding="utf-8"
    )
    assert "normalize_code(exc.code)" in worker, (
        "ingest_worker.record_failure must persist job.error_code through "
        "core.errors.normalize_code (single shared code-set)"
    )
    ask = (REPO_ROOT / "apps" / "api" / "app" / "ask" / "router.py").read_text(
        encoding="utf-8"
    )
    assert "error_detail(" in ask, (
        "ask router must build its error envelope via core.errors.error_detail"
    )


def test_error_detail_envelope_shape() -> None:
    detail = error_detail(ReasonCode.LLM_UNAVAILABLE)
    assert detail == {
        "error": "llm_unavailable",  # registered legacy wire value
        "message": ERROR_REGISTRY[ReasonCode.LLM_UNAVAILABLE].message,
    }
    # Unknown codes fail open, never raise: emitters owned by other streams
    # cannot be broken by this registry.
    assert error_detail("some_future_code") == {
        "error": "some_future_code",
        "message": "",
    }


# --- 5. required vs optional dependencies per enabled mode -----------------


def test_every_dependency_is_classified_for_every_mode() -> None:
    for dep in Dependency:
        assert dep in DEPENDENCY_MATRIX, f"{dep} missing from the dependency matrix"
        spec = DEPENDENCY_MATRIX[dep]
        assert spec.name is dep
        assert spec.required_modes & spec.optional_modes == set(), (
            f"{dep} classified required AND optional in the same mode"
        )
        assert spec.reason_code in ERROR_REGISTRY, f"{dep}: unregistered reason code"
        for mode in ServiceMode:
            requirement = classify_dependency(dep, mode)
            assert requirement in set(DependencyRequirement), f"{dep}/{mode} unclassified"
            if mode in spec.required_modes:
                assert requirement is DependencyRequirement.REQUIRED
            elif mode in spec.optional_modes:
                assert requirement is DependencyRequirement.OPTIONAL
            else:
                assert requirement is DependencyRequirement.UNUSED


def test_readiness_probe_set_is_fully_classified() -> None:
    """Every service ``/ready`` probes has a required/optional verdict."""
    readiness = (REPO_ROOT / "apps" / "api" / "app" / "adapters" / "readiness.py").read_text(
        encoding="utf-8"
    )
    probed = set(re.findall(r'"([a-z_]+)":\s*\w+\.probe\(', readiness))
    assert probed, "expected probe entries in readiness.py"
    unclassified = sorted(name for name in probed if Dependency(name) not in DEPENDENCY_MATRIX)
    assert unclassified == [], f"unclassified probed dependencies: {unclassified}"


def test_ask_and_search_mandatory_legs_map_to_required_dependencies() -> None:
    """Embedding/retrieval/generation legs fail closed, with safe reasons."""
    for mode in (ServiceMode.ASK, ServiceMode.SEARCH):
        required = required_dependencies(mode)
        assert Dependency.INFINITY in required, f"{mode}: embeddings must be required"
        assert Dependency.QDRANT in required, f"{mode}: retrieval must be required"
    assert Dependency.LLM_GATEWAY in required_dependencies(ServiceMode.ASK)
    assert classify_dependency(Dependency.LLM_GATEWAY, ServiceMode.SEARCH) is (
        DependencyRequirement.UNUSED
    ), "search must not claim the generation gateway"
    # Optional classifications stay honest: graph is never mandatory for the
    # non-graph modes.
    assert classify_dependency(Dependency.GRAPH_STORE, ServiceMode.ASK) is (
        DependencyRequirement.OPTIONAL
    )
