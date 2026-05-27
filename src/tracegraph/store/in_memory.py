"""Pure-Python, in-process graph store — the default backend.

Zero dependencies beyond the model, no server, no optional deps. It holds the
normalized trace in memory and answers the raw-graph traversal that ``explain``/RCA
need. The portable artifact remains the source of record; this store is rebuilt from
it on load.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from tracegraph import artifact
from tracegraph.model import Edge, EdgeType, NormalizedTrace, RawTrace, Step, Trace
from tracegraph.normalize import normalize, validate_normalized


class InMemoryStore:
    def __init__(self) -> None:
        self._trace: Trace | None = None
        self._steps: dict[str, Step] = {}
        self._edges: list[Edge] = []
        # adjacency for raw causal edges: effect -> [causes]
        self._caused_by: dict[str, list[str]] = {}

    # --- construction ---

    @classmethod
    def from_trace(cls, nt: NormalizedTrace) -> "InMemoryStore":
        """Load an already-normalized trace, validating both edge layers at the boundary."""
        validate_normalized(nt)
        store = cls()
        store.init_schema()
        store._trace = nt.trace
        store.upsert_nodes(nt.steps)
        store.upsert_edges(nt.edges)
        return store

    @classmethod
    def from_raw(cls, raw: RawTrace) -> "InMemoryStore":
        """Normalize a raw adapter output and load it. The intended ingestion path."""
        return cls.from_trace(normalize(raw))

    @classmethod
    def load_artifact(cls, path: str | Path) -> "InMemoryStore":
        return cls.from_trace(artifact.load(path))

    # --- GraphStore protocol ---

    def init_schema(self) -> None:  # nothing to do for an in-memory store
        pass

    def upsert_nodes(self, steps: list[Step]) -> None:
        for s in steps:
            self._steps[s.step_id] = s

    def upsert_edges(self, edges: list[Edge]) -> None:
        self._edges.extend(edges)
        for e in edges:
            if e.type is EdgeType.CAUSED_BY:
                self._caused_by.setdefault(e.src, []).append(e.dst)

    def ancestors(self, step_id: str) -> list[Step]:
        """All raw causal ancestors of ``step_id``, nearest cause first (BFS over CAUSED_BY)."""
        if step_id not in self._steps:
            raise KeyError(f"unknown step {step_id!r}")
        out: list[Step] = []
        seen: set[str] = {step_id}
        queue: deque[str] = deque(self._caused_by.get(step_id, []))
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            if cur not in self._steps:
                # A CAUSED_BY edge pointing at an unknown step means a corrupted raw
                # graph. explain/RCA must surface that, not silently drop the cause.
                raise KeyError(f"CAUSED_BY edge points to unknown step {cur!r}")
            out.append(self._steps[cur])
            queue.extend(self._caused_by.get(cur, []))
        return out

    def export_artifact(self, path: str | Path) -> None:
        artifact.save(self.trace(), path)

    def trace(self) -> NormalizedTrace:
        if self._trace is None:
            raise RuntimeError("store has no trace loaded")
        return NormalizedTrace(
            trace=self._trace,
            steps=list(self._steps.values()),
            edges=list(self._edges),
        )
