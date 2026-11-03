"""Which API key may be sent to which gateway host (single source of truth).

The credential is chosen from the *parsed* host of the base URL, never from a
substring, so a lookalike host (``openrouter.ai.evil.example``, a path or
userinfo trick) can not receive a key meant for someone else:

* OpenRouter host (``openrouter.ai`` / ``*.openrouter.ai``, no userinfo)
  -> ``OPENROUTER_API_KEY`` only, never ``LITELLM_API_KEY``; non-https -> ``None``.
* DeepSeek host (``deepseek.com`` / ``*.deepseek.com``, https, no userinfo)
  -> ``DEEPSEEK_API_KEY``, else ``LITELLM_API_KEY``.
* any other host -> ``LITELLM_API_KEY`` only, never ``OPENROUTER_API_KEY``.
* no suitable key -> ``None`` (callers skip / refuse; nothing is sent).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_GATEWAY_URL = "http://localhost:4000"


def _in_domain(url: str, domain: str) -> tuple[bool, bool]:
    """``(host is domain or a subdomain, scheme is https)``; userinfo/malformed -> False."""
    try:
        parts = urlsplit((url or "").strip())
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False, False
    # "@" anywhere in the authority is a userinfo trick (incl. backslash variants).
    if "@" in parts.netloc:
        return False, False
    host = parts.hostname or ""  # urlsplit lowercases it
    return (host == domain or host.endswith("." + domain)), parts.scheme == "https"


def _is_https_host(url: str, domain: str) -> bool:
    in_domain, https = _in_domain(url, domain)
    return in_domain and https


def is_openrouter_url(url: str) -> bool:
    return _is_https_host(url, "openrouter.ai")


def _key(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def credential_for_url(url: str, env: Mapping[str, str] | None = None) -> str | None:
    """Return the only API key allowed for ``url``, or ``None``."""
    env = os.environ if env is None else env
    in_or, or_https = _in_domain(url, "openrouter.ai")
    if in_or:  # OpenRouter family on any scheme: cleartext is refused, never LITELLM's key
        return _key(env, "OPENROUTER_API_KEY") if or_https else None
    if _is_https_host(url, "deepseek.com"):
        return _key(env, "DEEPSEEK_API_KEY") or _key(env, "LITELLM_API_KEY")
    return _key(env, "LITELLM_API_KEY")


def resolve_gateway(
    env: Mapping[str, str] | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str | None]:
    """Resolve ``(base_url, api_key)`` for the chat/embedding gateway.

    URL: explicit ``base_url``; else the OpenRouter default when
    ``OPENROUTER_API_KEY`` is set; else ``LITELLM_URL``; else the local default.
    Key: explicit ``api_key`` (the caller's own choice), else the host-bound key.
    """
    env = os.environ if env is None else env
    if base_url:
        url = base_url
    elif env.get("OPENROUTER_API_KEY"):
        url = OPENROUTER_BASE_URL
    else:
        url = env.get("LITELLM_URL") or DEFAULT_GATEWAY_URL
    url = url.rstrip("/")
    return url, api_key or credential_for_url(url, env)
