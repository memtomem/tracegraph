"""The adapter interface: turn an external trace source into a :class:`RawTrace`.

An adapter never computes the derived tree — it only reconstructs the raw causal graph
(steps + ``CAUSED_BY`` edges). ``normalize`` does the rest.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tracegraph.model import RawTrace


@runtime_checkable
class TraceAdapter(Protocol):
    def discover(self) -> list[str]:
        """Best-effort list of available trace ids (may be unsupported by a source)."""
        ...

    def ingest(self, trace_id: str) -> RawTrace:
        """Reconstruct one trace's raw causal graph. Raises ``KeyError`` if not found."""
        ...
