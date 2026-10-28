"""SSRF guard: web fetches never reach loopback, private or link-local addresses."""

import pytest
from connectors.adapters.web_connector import OutOfScopeRedirect, _assert_public_host

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6333/collections",
        "http://localhost:8000/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]:8000/",
        "http://0.0.0.0/",
    ],
)
def test_non_public_targets_are_refused(url):
    with pytest.raises(OutOfScopeRedirect):
        _assert_public_host(url)


def test_public_literal_address_is_allowed():
    _assert_public_host("https://8.8.8.8/")
