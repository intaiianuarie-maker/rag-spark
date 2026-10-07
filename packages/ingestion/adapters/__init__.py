"""PD-001 parser adapters (M2-001a).

Each module isolates one third-party parser and normalizes its output to our
:class:`ingestion.interfaces.DocumentElement` tree. Vendor types never cross
this boundary; every module exposes ``PARSER`` and ``create_parser()`` per the
M0-004 harness convention (`tests/README.md`, `expected_elements.json`).
"""
