"""Optional Kùzu-backed graph store — the openCypher cache the spec advertises.

This is the **accelerator**, never the system of record. The JSON artifact remains
authoritative (see :mod:`tracegraph.artifact`); a ``KuzuStore`` is rebuilt from it on load
and can be thrown away. The point of this backend is to *prove* the ``PathPattern`` spec
compiles to standard openCypher — equivalence with the pure-Python matcher is what makes
the abstraction honest — so the bulk of the file is a faithful schema and a thin query
runner, not a separate analysis library.

In-memory by default (``kuzu.Database(":memory:")``) so it matches the InMemoryStore's
ephemerality. Pass ``path=`` to persist; the on-disk format is **not** stable across Kùzu
versions (the project's repo was archived October 2025, and 0.11.3 is the pinned floor),
so durable durability is the JSON artifact, not the DB directory.

Requires the ``[cypher]`` extra (``pip install tracegraph[cypher]``). The import will fail
cleanly with the ModuleNotFoundError raised by ``kuzu`` itself; the package's default install
never touches this module.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import kuzu

from tracegraph import artifact
from tracegraph.analysis.patterns import PathPattern, compile_to_cypher
from tracegraph.model import (
    Edge,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize, validate_normalized

# Property layout for the Step node table. Kept beside the schema so adding a model field
# is a single-source change; the constructor reads this list to bind ``$params`` for INSERT.
_STEP_FIELDS: tuple[str, ...] = (
    "step_id",
    "trace_id",
    "seq",
    "ts",
    "kind",
    "source",
    "name",
    "status",
    "error_msg",
    "projection_lossy",
)

_TRACE_FIELDS: tuple[str, ...] = ("trace_id", "source_kind", "thread_id", "status")

_SCHEMA_DDL: tuple[str, ...] = (
    # Step: STRING for the enum columns so the Cypher compiler can compare against
    # ``StepKind.value`` / ``StepStatus.value`` directly (Kùzu has no native enums).
    "CREATE NODE TABLE Step ("
    "step_id STRING, trace_id STRING, seq INT64, ts STRING, "
    "kind STRING, source STRING, name STRING, status STRING, "
    "error_msg STRING, projection_lossy BOOLEAN, "
    "PRIMARY KEY (step_id))",
    "CREATE NODE TABLE Trace ("
    "trace_id STRING, source_kind STRING, thread_id STRING, status STRING, "
    "PRIMARY KEY (trace_id))",
    "CREATE REL TABLE CAUSED_BY (FROM Step TO Step)",
    "CREATE REL TABLE TREE_PARENT (FROM Step TO Step)",
    "CREATE REL TABLE BELONGS_TO (FROM Step TO Trace)",
)


class KuzuStore:
    """Implementation of :class:`~tracegraph.store.base.GraphStore` backed by Kùzu."""

    def __init__(self, *, path: str | Path | None = None) -> None:
        # ":memory:" is Kùzu's in-process sentinel; matches InMemoryStore's no-files default.
        self._db = kuzu.Database(":memory:" if path is None else str(path))
        self._conn = kuzu.Connection(self._db)
        self._trace: Trace | None = None

    # --- construction ---

    @classmethod
    def from_trace(cls, nt: NormalizedTrace, *, path: str | Path | None = None) -> "KuzuStore":
        """Load a normalized trace. Validates both edge layers at the boundary (same as
        InMemoryStore.from_trace) so a corrupt artifact can't enter the DB."""
        validate_normalized(nt)
        store = cls(path=path)
        store.init_schema()
        store._trace = nt.trace
        store._insert_trace(nt.trace)
        store.upsert_nodes(nt.steps)
        store.upsert_edges(nt.edges)
        return store

    @classmethod
    def from_raw(cls, raw: RawTrace, *, path: str | Path | None = None) -> "KuzuStore":
        return cls.from_trace(normalize(raw), path=path)

    @classmethod
    def load_artifact(cls, path: str | Path) -> "KuzuStore":
        # ``path`` here is the **artifact** JSON, not the Kùzu DB directory — the artifact
        # is the system of record, so loading by definition goes through it.
        return cls.from_trace(artifact.load(path))

    # --- GraphStore protocol ---

    def init_schema(self) -> None:
        for ddl in _SCHEMA_DDL:
            self._conn.execute(ddl)

    def upsert_nodes(self, steps: list[Step]) -> None:
        # Kùzu has no batch CREATE for parameterized rows in 0.11; one statement per step
        # is fine at MVP volumes (a trace is hundreds of steps, not millions).
        placeholders = ", ".join(f"{f}: ${f}" for f in _STEP_FIELDS)
        stmt = f"CREATE (:Step {{{placeholders}}})"
        for s in steps:
            self._conn.execute(stmt, _step_params(s))

    def upsert_edges(self, edges: list[Edge]) -> None:
        for e in edges:
            self._insert_edge(e)

    def ancestors(self, step_id: str) -> list[Step]:
        """Raw CAUSED_BY ancestors of ``step_id``, nearest-cause first.

        Implementation: pull the full ancestor subgraph in one parameterized var-length
        match, then BFS in Python. The BFS step preserves the in-memory store's edge-order
        semantics on fan-in (Cypher gives no row-order guarantee, so we'd otherwise diverge
        on multi-parent steps). For linear traces — the LangGraph default — the order is
        identical either way.
        """
        if not self._step_exists(step_id):
            raise KeyError(f"unknown step {step_id!r}")

        # adjacency restricted to the relevant subgraph: every edge reachable from ``step_id``.
        rows = _collect(
            self._conn.execute(
                "MATCH (start:Step)-[:CAUSED_BY*0..]->(effect:Step)-[:CAUSED_BY]->(cause:Step) "
                "WHERE start.step_id = $sid "
                "RETURN effect.step_id, cause.step_id",
                {"sid": step_id},
            )
        )
        adjacency: dict[str, list[str]] = {}
        for effect_id, cause_id in rows:
            adjacency.setdefault(effect_id, []).append(cause_id)
        # Match the InMemoryStore's per-node child order: it inherits CAUSED_BY edge
        # insertion order, which after :func:`normalize` canonicalization is
        # ``(effect.seq, cause.seq, src, dst)``. Cypher gives us no row-order guarantee,
        # so reproduce that sort here.
        steps_by_id = {s.step_id: s for s in self._load_all_steps()}
        for effect_id, causes in adjacency.items():
            causes.sort(
                key=lambda cid: (steps_by_id[cid].seq, cid)
            )

        out: list[Step] = []
        seen: set[str] = {step_id}
        queue: deque[str] = deque(adjacency.get(step_id, []))
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            out.append(steps_by_id[cur])
            queue.extend(adjacency.get(cur, []))
        return out

    def export_artifact(self, path: str | Path) -> None:
        artifact.save(self.trace(), path)

    def trace(self) -> NormalizedTrace:
        """Reconstruct the loaded trace from the DB by re-running :func:`normalize`.

        We **only** pull the raw layer (steps + CAUSED_BY) from Kùzu and let normalize()
        re-derive ``BELONGS_TO``/``TREE_PARENT``/``projection_lossy``. That collapses two
        round-trip invariants into one source of truth: KuzuStore is byte-stable iff
        ``normalize()`` is deterministic (it is). The TREE_PARENT and BELONGS_TO rows that
        live in the DB are still useful for ad-hoc Cypher against the derived layer; they
        just aren't load-bearing for artifact round-trip.
        """
        if self._trace is None:
            raise RuntimeError("store has no trace loaded")
        raw_steps = [
            s.model_copy(update={"projection_lossy": False}) for s in self._load_all_steps()
        ]
        caused_by_pairs = _collect(
            self._conn.execute(
                "MATCH (a:Step)-[:CAUSED_BY]->(b:Step) RETURN a.step_id, b.step_id"
            )
        )
        raw_edges = [
            Edge(type=EdgeType.CAUSED_BY, src=src, dst=dst) for src, dst in caused_by_pairs
        ]
        return normalize(RawTrace(trace=self._trace, steps=raw_steps, causal_edges=raw_edges))

    # --- pattern matching (the reason Kùzu earns its weight) ---

    def find_matches(self, pattern: PathPattern) -> list[list[str]]:
        """Run a :class:`PathPattern` as compiled openCypher.

        Returns the **same list, in the same order**, as
        :func:`tracegraph.analysis.find_matches` on the in-memory store. Pure-Python's
        traversal visits outer steps in canonical step order and expands each child level
        in canonical CAUSED_BY order — both of which, after :func:`normalize` canonicalization,
        sort to ``(seq, step_id)`` per position. So we reproduce that ordering on the rows
        Kùzu returns. (Cypher itself gives no row-order guarantee; without this sort the
        backends would silently disagree.)
        """
        if not pattern.steps:
            # Match the pure-Python convention so callers can be backend-agnostic; the
            # compile step would raise, but raising here would break that contract.
            return []
        q = compile_to_cypher(pattern)
        rows = _collect(self._conn.execute(q.cypher, q.params))
        steps_by_id = {s.step_id: s for s in self._load_all_steps()}
        rows.sort(key=lambda row: tuple((steps_by_id[sid].seq, sid) for sid in row))
        return [list(row) for row in rows]

    # --- internals ---

    def _insert_trace(self, t: Trace) -> None:
        placeholders = ", ".join(f"{f}: ${f}" for f in _TRACE_FIELDS)
        stmt = f"CREATE (:Trace {{{placeholders}}})"
        self._conn.execute(stmt, _trace_params(t))

    def _insert_edge(self, e: Edge) -> None:
        # BELONGS_TO targets a Trace node; the causal/tree edges stay Step→Step.
        target_label = "Trace" if e.type is EdgeType.BELONGS_TO else "Step"
        stmt = (
            f"MATCH (src:Step), (dst:{target_label}) "
            f"WHERE src.step_id = $src AND dst.{'trace_id' if target_label == 'Trace' else 'step_id'} = $dst "
            f"CREATE (src)-[:{e.type.value}]->(dst)"
        )
        self._conn.execute(stmt, {"src": e.src, "dst": e.dst})

    def _step_exists(self, step_id: str) -> bool:
        rows = _collect(
            self._conn.execute(
                "MATCH (s:Step) WHERE s.step_id = $sid RETURN s.step_id",
                {"sid": step_id},
            )
        )
        return bool(rows)

    def _load_all_steps(self) -> list[Step]:
        rows = _collect(
            self._conn.execute(
                "MATCH (s:Step) RETURN " + ", ".join(f"s.{f}" for f in _STEP_FIELDS)
            )
        )
        return [_step_from_row(r) for r in rows]


# --- helpers (module-level so they're easy to unit-test if needed) ---


def _step_params(s: Step) -> dict[str, Any]:
    """Bind a Step to the names declared in ``_STEP_FIELDS`` (enums → ``.value`` strings)."""
    return {
        "step_id": s.step_id,
        "trace_id": s.trace_id,
        "seq": s.seq,
        "ts": s.ts,
        "kind": s.kind.value,
        "source": s.source.value,
        "name": s.name,
        "status": s.status.value,
        "error_msg": s.error_msg,
        "projection_lossy": s.projection_lossy,
    }


def _trace_params(t: Trace) -> dict[str, Any]:
    return {
        "trace_id": t.trace_id,
        "source_kind": t.source_kind,
        "thread_id": t.thread_id,
        "status": t.status.value,
    }


def _step_from_row(row: list[Any]) -> Step:
    """Inverse of ``_step_params``: a SELECT row in ``_STEP_FIELDS`` order back to a Step."""
    by_name = dict(zip(_STEP_FIELDS, row, strict=True))
    return Step(
        step_id=by_name["step_id"],
        trace_id=by_name["trace_id"],
        seq=by_name["seq"],
        ts=by_name["ts"],
        kind=StepKind(by_name["kind"]),
        source=StepSource(by_name["source"]),
        name=by_name["name"],
        status=StepStatus(by_name["status"]),
        error_msg=by_name["error_msg"],
        projection_lossy=by_name["projection_lossy"],
    )


def _collect(result: Any) -> list[list[Any]]:
    """Drain a Kùzu QueryResult into a plain list of rows."""
    rows: list[list[Any]] = []
    while result.has_next():
        rows.append(result.get_next())
    return rows
