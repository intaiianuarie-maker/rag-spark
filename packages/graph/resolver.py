"""Entity resolution & canonicalization framework (Section 22).

Merges duplicate and alias entity mentions into canonical entities, preserving
all aliases and aggregating chunk provenance.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from .interfaces import Entity


class EntityResolver(ABC):
    """Abstract entity resolution strategy."""

    @abstractmethod
    def resolve_entities(self, entities: Sequence[Entity]) -> list[Entity]:
        """Group and merge duplicate entity mentions into canonical entities."""
        raise NotImplementedError


def _normalize_name(name: str) -> str:
    # Lowercase, strip punctuation and common corporate suffixes
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    # Strip common corporate endings
    for suffix in (" corporation", " corp", " incorporated", " inc", " llc", " ltd", " gmbh"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


class SimpleEntityResolver(EntityResolver):
    """Rule- and alias-based entity resolution."""

    def __init__(self, manual_aliases: dict[str, str] | None = None) -> None:
        # e.g. {"msft": "microsoft", "goog": "google"}
        self._manual_aliases = {k.lower(): v.lower() for k, v in (manual_aliases or {}).items()}

    def resolve_entities(self, entities: Sequence[Entity]) -> list[Entity]:
        groups: dict[tuple[str, str, str], list[Entity]] = {}

        for ent in entities:
            norm = _normalize_name(ent.name)
            if norm in self._manual_aliases:
                norm = self._manual_aliases[norm]
            key = (ent.workspace_id, str(ent.type).upper(), norm)
            groups.setdefault(key, []).append(ent)

        resolved: list[Entity] = []
        for (_ws, _type, _norm), members in groups.items():
            if len(members) == 1:
                resolved.append(members[0])
                continue

            # Pick the most complete name as canonical
            primary = max(members, key=lambda m: (len(m.name), len(m.description)))
            all_aliases = set(primary.aliases)
            all_chunks = set(primary.chunk_ids)
            all_docs = set(primary.document_ids)
            merged_props = dict(primary.properties)

            for other in members:
                if other.id != primary.id:
                    all_aliases.add(other.name)
                all_aliases.update(other.aliases)
                all_chunks.update(other.chunk_ids)
                all_docs.update(other.document_ids)
                for k, v in other.properties.items():
                    if k not in merged_props:
                        merged_props[k] = v

            all_aliases.discard(primary.name)

            resolved.append(
                Entity(
                    id=primary.id,
                    name=primary.name,
                    type=primary.type,
                    description=primary.description,
                    workspace_id=primary.workspace_id,
                    aliases=sorted(all_aliases),
                    chunk_ids=sorted(all_chunks),
                    document_ids=sorted(all_docs),
                    properties=merged_props,
                )
            )

        return resolved
