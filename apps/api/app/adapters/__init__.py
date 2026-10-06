"""Adapters: the only place vendor/project-specific SDKs may be imported.

Each module here probes a backing service for ``GET /ready``. They are thin,
replaceable and return our :class:`core.health.DependencyStatus` type.
"""
