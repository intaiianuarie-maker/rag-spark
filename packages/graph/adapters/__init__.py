"""Adapter implementations for GraphRAG (V2 work package G1).

Adapters live in their own sub-package so the seam boundary is explicit: the
graph core (:mod:`graph.extractor` / :mod:`graph.interfaces`) stays free of any
vendor SDK — it only depends on the public :mod:`models.interfaces.LLMProvider`
contract. No vendor SDK crosses that boundary.
"""

from __future__ import annotations

from .lightrag_extractor import LLMBasedGraphExtractor

__all__ = ["LLMBasedGraphExtractor"]
