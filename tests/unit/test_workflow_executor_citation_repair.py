"""Workflow output gets the same ``<uuid> (page N)`` citation repair as /ask."""

from __future__ import annotations

import json

import pytest
from workflow_engine.production_executor import ProductionNodeExecutor

pytestmark = pytest.mark.unit

KNOWN = "123e4567-e89b-12d3-a456-426614174000"
UNKNOWN = "223e4567-e89b-12d3-a456-426614174000"


def _out(cid: str) -> str:
    return json.dumps({"answer": "a", "claims": [{"text": "x", "chunk_ids": [cid]}]})


def test_known_uuid_with_page_suffix_is_normalised() -> None:
    payload, _ = ProductionNodeExecutor._split_output_payload(_out(f"{KNOWN} (page 3)"), [KNOWN])
    assert payload["claims"][0]["chunk_ids"] == [KNOWN]


def test_unknown_uuid_is_not_repaired() -> None:
    cid = f"{UNKNOWN} (page 3)"
    payload, _ = ProductionNodeExecutor._split_output_payload(_out(cid), [KNOWN])
    assert payload["claims"][0]["chunk_ids"] == [cid]
