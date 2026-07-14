"""The pluggable query backend interface — deliberately small.

The store does **not** expose a ``query(cypher)`` method: that would leak Cypher
upward and make the pure-Python path a second-class citizen. Instead, analyses are
expressed as *operations* the store implements; each backend supplies its own
implementation (pure-Python traversal here; compiled Cypher in the optional LadybugDB
backend for pattern matching).

Pattern matching intentionally stays out of the minimal ``GraphStore`` protocol. It is a
separate backend capability (pure-Python functions for the default path; ``LadybugStore`` adds
``find_matches``) so RCA/diff callers do not depend on a Cypher-capable backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from tracegraph.model import Edge, NormalizedTrace, Step


@runtime_checkable
class GraphStore(Protocol):
    def init_schema(self) -> None: ...

    def upsert_nodes(self, steps: list[Step]) -> None: ...

    def upsert_edges(self, edges: list[Edge]) -> None:
        """Store ALL edges, including every raw CAUSED_BY (not just the tree parent)."""
        ...

    def ancestors(self, step_id: str) -> list[Step]:
        """Raw CAUSED_BY traversal from ``step_id`` toward root causes (the RCA primitive)."""
        ...

    def export_artifact(self, path: str | Path) -> None: ...

    @classmethod
    def load_artifact(cls, path: str | Path) -> "GraphStore": ...

    def trace(self) -> NormalizedTrace:
        """Return the loaded trace as a NormalizedTrace (for rendering/diffing)."""
        ...
