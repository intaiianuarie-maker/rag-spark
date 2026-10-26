"""Compatibility exports for the shared connector configuration SQL adapter."""

from connectors.adapters.config_loader import (
    load_persisted_sources as load_persisted_sources,
)
from connectors.adapters.config_loader import (
    make_db_source_loader as make_db_source_loader,
)
