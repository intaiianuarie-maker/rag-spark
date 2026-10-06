"""Harness hygiene: no model weights, no API keys, no vendor SDKs in tests/.

TASK-M0-004 acceptance criterion "No test downloads model weights or requires
API keys" is enforced here as a permanent self-check, alongside the CI vendor
isolation guard (which only scans apps/ and packages/). Also pins the harness
inventory: the committed fixture set must not silently shrink.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Vendor SDKs / weight-fetching libraries that must never appear in tests/.
FORBIDDEN_IMPORTS = (
    "openai",
    "anthropic",
    "sentence_transformers",
    "transformers",
    "torch",
    "vllm",
    "ollama",
    "cohere",
    "jina",
    "llama_index",
    "haystack",
)

IMPORT_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+(" + "|".join(FORBIDDEN_IMPORTS) + r")\b", re.MULTILINE
)

API_KEY_PATTERN = re.compile(r"(?:api[_-]?key|secret|token)\s*=\s*os\.environ", re.IGNORECASE)


def _test_sources(repo_root: Path) -> list[Path]:
    tests_dir = repo_root / "tests"
    return sorted(
        path
        for path in tests_dir.rglob("*.py")
        if "__pycache__" not in path.parts and path.name != "_generate.py"
    )


def test_no_vendor_sdk_or_weight_library_imported_by_tests(repo_root: Path) -> None:
    offenders = []
    for path in _test_sources(repo_root):
        source = path.read_text(encoding="utf-8")
        for match in IMPORT_PATTERN.finditer(source):
            offenders.append(f"{path.relative_to(repo_root)}: {match.group(0).strip()}")
    assert not offenders, "tests must not import vendor/model SDKs:\n" + "\n".join(offenders)


def test_no_test_requires_an_api_key(repo_root: Path) -> None:
    offenders = []
    for path in _test_sources(repo_root):
        for match in API_KEY_PATTERN.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(repo_root)}: {match.group(0)}")
    assert not offenders, "tests must not read API keys from the environment:\n" + "\n".join(
        offenders
    )


def test_harness_inventory_matches_the_task_requirements(repo_root: Path) -> None:
    """The committed harness must keep every artefact TASK-M0-004 asks for."""
    required = [
        "tests/conftest.py",
        "tests/README.md",
        "tests/fixtures/harness_fakes.py",
        "tests/fixtures/service_probe.py",
        "tests/fixtures/pd001/expected_elements.json",
        "tests/fixtures/scripts/m004_probe_checks.py",
        "tests/fixtures/scripts/m004_ready_degraded.py",
        "tests/fixtures/scripts/m004_proxy_branches.sh",
        "tests/fixtures/scripts/m004_stubs.py",
        "tests/fixtures/scripts/health_guard_check.mjs",
        "tests/unit/test_rrf_determinism.py",
        "tests/unit/test_acl_before_rank.py",
        "tests/contracts/test_vector_store_contract.py",
        "tests/contracts/test_lexical_store_contract.py",
        "tests/contracts/test_document_parser_contract.py",
        "tests/contracts/test_embedding_provider_contract.py",
        "tests/contracts/test_reranker_contract.py",
        "tests/contracts/test_retrieval_orchestrator_contract.py",
        "tests/contracts/test_llm_provider_contract.py",
        "tests/contracts/test_connector_contract.py",
        "tests/integration/test_security_placeholders.py",
        "packages/ingestion/contract_tests.py",
        "packages/retrieval/contract_tests.py",
        "packages/models/contract_tests.py",
        "packages/connectors/contract_tests.py",
    ]
    missing = [item for item in required if not (repo_root / item).is_file()]
    assert not missing, f"harness files missing: {missing}"


def test_six_pd001_fixture_files_are_present(repo_root: Path) -> None:
    pd001 = repo_root / "tests" / "fixtures" / "pd001"
    expected = {
        "pdf_text_multipage.pdf",
        "pdf_table.pdf",
        "docx_headings_table.docx",
        "html_article_nav.html",
        "markdown_structure.md",
        "csv_multilingual.csv",
    }
    present = {
        path.name
        for path in pd001.iterdir()
        if path.is_file() and path.suffix in {".pdf", ".docx", ".html", ".md", ".csv"}
    }
    assert expected <= present, f"missing PD-001 fixtures: {sorted(expected - present)}"


def test_contract_checkers_are_reachable_from_the_packages(repo_root: Path) -> None:
    """Every adapter interface must have a contract module next to it."""
    from connectors.contract_tests import check_connector_contract
    from ingestion.contract_tests import check_document_parser_contract
    from models.contract_tests import check_llm_provider_contract
    from retrieval.contract_tests import (
        check_embedding_provider_contract,
        check_lexical_store_contract,
        check_reranker_contract,
        check_retrieval_results_contract,
        check_rrf_determinism,
        check_vector_store_contract,
    )

    for function in (
        check_connector_contract,
        check_document_parser_contract,
        check_llm_provider_contract,
        check_embedding_provider_contract,
        check_lexical_store_contract,
        check_reranker_contract,
        check_retrieval_results_contract,
        check_rrf_determinism,
        check_vector_store_contract,
    ):
        assert callable(function), function
    assert (repo_root / "packages").is_dir()
