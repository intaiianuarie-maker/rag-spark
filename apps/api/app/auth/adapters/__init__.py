"""Auth external-service adapters.

Vendor/project-specific clients (currently Redis) live only here, keeping the
rest of the auth module free of third-party SDK imports per
docs/architecture/module-boundaries.md and the CI vendor-import guard.
"""
