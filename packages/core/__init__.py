"""Knowledge OS core: shared configuration, logging, auth stubs and types.

This package is intentionally dependency-light (stdlib + pydantic) so it can be
imported by the API, the worker and every other package without pulling in
vendor SDKs. Vendor/project-specific code must stay behind `adapters/`.

Reuse basis: pydantic / pydantic-settings (MIT) for typed configuration.
Custom code here is limited to the small product-specific glue (field set,
JSON log formatter, ACL dataclasses) described in `docs/reuse/REUSE_STRATEGY.md`.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
