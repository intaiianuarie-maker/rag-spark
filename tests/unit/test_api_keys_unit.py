"""Unit tests for API-key material (M1-001c) — no database, no Redis."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTH_JWT_SECRET", "test-secret-not-for-production-0123456789")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MIGRATIONS_ON_BOOT", "false")

from app.auth.keys import (
    API_KEY_PREFIX,
    extract_prefix,
    generate_key_material,
    looks_like_api_key,
)
from app.auth.passwords import verify_password

pytestmark = pytest.mark.unit


def test_generated_key_shape_is_prefix_plus_hash() -> None:
    material = generate_key_material()
    assert material.full_key.startswith(API_KEY_PREFIX)
    assert len(material.prefix) == 12
    assert material.full_key.startswith(f"{API_KEY_PREFIX}{material.prefix}_")
    assert material.key_hash.startswith("$argon2")
    assert material.full_key not in material.key_hash  # secret never stored raw
    assert verify_password(material.key_hash, material.full_key) is True
    assert verify_password(material.key_hash, material.full_key + "x") is False


def test_prefix_extraction_and_shape_validation() -> None:
    material = generate_key_material()
    assert extract_prefix(material.full_key) == material.prefix
    assert extract_prefix("not-a-key") is None
    assert extract_prefix("kio_") is None
    assert extract_prefix("kio_abc") is None
    assert looks_like_api_key(material.full_key) is True
    assert looks_like_api_key("eyJhbGciOiJIUzI1NiJ9.payload.sig") is False


def test_prefixes_are_unique_across_many_keys() -> None:
    prefixes = {generate_key_material().prefix for _ in range(200)}
    assert len(prefixes) == 200
