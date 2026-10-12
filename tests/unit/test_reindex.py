"""Unit tests for reindexing vectors and validation (RP-10 / QF-10)."""

import math
import pytest
from scripts.reindex_all_chunks import validate_vector


def test_validate_vector_valid():
    vec = [0.1, -0.2, 0.5, 0.0]
    # 4 dimensions, has non-zero finite values
    validate_vector(vec, 4)


def test_validate_vector_rejects_dimension_mismatch():
    vec = [0.1, 0.2, 0.3]
    with pytest.raises(ValueError, match="Vector dimension mismatch"):
        validate_vector(vec, 4)


def test_validate_vector_rejects_all_zeros():
    vec = [0.0] * 1024
    with pytest.raises(ValueError, match="Manufactured all-zero vector rejected"):
        validate_vector(vec, 1024)


def test_validate_vector_rejects_nan():
    vec = [0.1, float("nan"), 0.3, 0.4]
    with pytest.raises(ValueError, match="non-finite"):
        validate_vector(vec, 4)


def test_validate_vector_rejects_inf():
    vec = [0.1, float("inf"), 0.3, 0.4]
    with pytest.raises(ValueError, match="non-finite"):
        validate_vector(vec, 4)
