"""Optional LadybugDB-backed graph store — the Cypher cache the spec advertises.

This is the **accelerator**, never the system of record. The JSON artifact remains
authoritative (see :mod:`tracegraph.artifact`); a ``LadybugStore`` is rebuilt from it on load
and can be thrown away. The point of this backend is to *prove* the ``PathPattern`` spec
compiles to standard Cypher — equivalence with the pure-Python matcher is what makes
the abstraction honest — so the bulk of the file is a faithful schema and a thin query
runner, not a separate analysis library.

In-memory by default (``ladybug.Database(":memory:")``) so it matches the InMemoryStore's
ephemerality. Pass ``path=`` to persist. Database caches are version-local and rebuildable;
durable persistence is the JSON artifact, not the DB directory.

Requires the ``[cypher]`` extra (``pip install agent-tracegraph[cypher]``). The import will fail
cleanly with the ModuleNotFoundError raised by ``ladybug`` itself; the package's default install
never touches this module.
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from typing import Any

import ladybug

from tracegraph import artifact
from tracegraph.analysis.patterns import (
    PathPattern,
    UncompilablePattern,
    compile_to_cypher,
)
from tracegraph.analysis.patterns import find_matches as py_find_matches
from tracegraph.model import (
    Edge,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepEvidence,
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
    "evidence_json",
    "projection_lossy",
)

_TRACE_FIELDS: tuple[str, ...] = (
    "trace_id",
    "source_kind",
    "run_id",
    "thread_id",
    "status",
    "causal_fidelity",
    "links_preserved",
    "decision_evidence_json",
)

_SCHEMA_DDL: tuple[str, ...] = (
    # Step: STRING for the enum columns so the Cypher compiler can compare against
    # ``StepKind.value`` / ``StepStatus.value`` directly (Ladybug has no native enums).
    "CREATE NODE TABLE Step ("
    "step_id STRING, trace_id STRING, seq INT64, ts STRING, "
    "kind STRING, source STRING, name STRING, status STRING, "
    "error_msg STRING, evidence_json STRING, projection_lossy BOOLEAN, "
    "PRIMARY KEY (step_id))",
    "CREATE NODE TABLE Trace ("
    "trace_id STRING, source_kind STRING, run_id STRING, thread_id STRING, status STRING, "
    "causal_fidelity STRING, links_preserved BOOLEAN, decision_evidence_json STRING, "
    "PRIMARY KEY (trace_id))",
    "CREATE REL TABLE CAUSED_BY (FROM Step TO Step, origin STRING)",
    "CREATE REL TABLE TREE_PARENT (FROM Step TO Step)",
    "CREATE REL TABLE BELONGS_TO (FROM Step TO Trace)",
)


class LadybugStore:
    """Implementation of :class:`~tracegraph.store.base.GraphStore` backed by LadybugDB."""

    def __init__(self, *, path: str | Path | None = None) -> None:
        # ":memory:" is Ladybug's in-process sentinel; matches InMemoryStore's no-files default.
        db = ladybug.Database(":memory:" if path is None else str(path))
        try:
            conn = ladybug.Connection(db)
        except BaseException:
            # A failed connection leaves no store for anyone to close — release the
            # database here or it stays open for the process lifetime.
            db.close()
            raise
        self._db = db
        self._conn = conn
        self._closed = False
        self._trace: Trace | None = None
        # Read caches over the DB contents, invalidated by the corresponding upsert. The
        # store is an accelerator over an immutable-once-loaded trace, so repeated queries
        # (ancestors per failure, find_matches per preset) shouldn't re-dump the graph.
        self._steps_cache: list[Step] | None = None
        self._causes_cache: dict[str, list[str]] | None = None
        self._trace_cache: NormalizedTrace | None = None
        #: True iff the most recent find_matches() could not compile to faithful Cypher
        #: (unbounded/over-cap gap) and ran the pure-Python matcher instead. Lets the CLI
        #: tell the user the accelerator deferred — the results are identical either way.
        self.fell_back_to_python = False

    # --- lifecycle ---

    def close(self) -> None:
        """Release the underlying connection and database. Idempotent.

        The store holds a live LadybugDB connection; a caller creating many stores (one per
        trace in a directory sweep) must close each one or the process accumulates open
        databases. Using the store after close raises ``RuntimeError``.
        """
        if self._closed:
            return
        self._closed = True
        self._steps_cache = None
        self._causes_cache = None
        self._trace_cache = None
        try:
            self._conn.close()
        finally:
            # The database must be released even if the connection close raises.
            self._db.close()

    def __enter__(self) -> "LadybugStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("LadybugStore is closed")

    # --- construction ---

    @classmethod
    def from_trace(cls, nt: NormalizedTrace, *, path: str | Path | None = None) -> "LadybugStore":
        """Load a normalized trace. Validates both edge layers at the boundary (same as
        InMemoryStore.from_trace) so a corrupt artifact can't enter the DB."""
        validate_normalized(nt)
        store = cls(path=path)
        try:
            store.init_schema()
            # Deep-copy the header, matching InMemoryStore.from_trace. Holding the caller's
            # object by reference let a later mutation of the input change what trace()
            # returns while the persisted Trace row still held the original values, so the
            # store and its own database disagreed about the same trace.
            store._trace = nt.trace.model_copy(deep=True)
            store._insert_trace(nt.trace)
            store.upsert_nodes(nt.steps)
            store.upsert_edges(nt.edges)
        except BaseException:
            # Don't leak a live DB/connection behind a failed constructor — the caller
            # never receives the store, so it could never close it.
            store.close()
            raise
        return store

    @classmethod
    def from_raw(cls, raw: RawTrace, *, path: str | Path | None = None) -> "LadybugStore":
        return cls.from_trace(normalize(raw), path=path)

    @classmethod
    def load_artifact(cls, path: str | Path) -> "LadybugStore":
        # ``path`` here is the **artifact** JSON, not a Ladybug DB directory — the artifact
        # is the system of record, so loading by definition goes through it.
        return cls.from_trace(artifact.load(path))

    # --- GraphStore protocol ---

    def init_schema(self) -> None:
        for ddl in _SCHEMA_DDL:
            self._conn.execute(ddl)

    def upsert_nodes(self, steps: list[Step]) -> None:
        """Insert a batch of steps in one UNWIND statement.

        The batch is **atomic**: if any row fails (e.g. a duplicate ``step_id`` primary
        key), LadybugDB rolls the whole statement back and no rows land. This is a
        deliberate contract change from the earlier per-row loop, which left the
        already-inserted prefix behind on failure — all-or-nothing is the behavior a
        rebuildable accelerator cache actually wants (a half-loaded trace is never
        queryable as if complete). Same contract for :meth:`upsert_edges`.
        """
        self._check_open()
        if not steps:
            return
        # One UNWIND per batch instead of one statement per step — bulk load is a single
        # round-trip through the query engine.
        placeholders = ", ".join(f"{f}: r.{f}" for f in _STEP_FIELDS)
        self._conn.execute(
            f"UNWIND $rows AS r CREATE (:Step {{{placeholders}}})",
            {"rows": [_step_params(s) for s in steps]},
        )
        self._steps_cache = None
        self._trace_cache = None

    def upsert_edges(self, edges: list[Edge]) -> None:
        self._check_open()
        if not edges:
            return
        # One UNWIND per edge type (the relationship label and target table differ per
        # type), preserving each type's insertion order within its batch. The per-type
        # statements run inside one transaction so a mixed batch keeps the same
        # all-or-nothing contract as upsert_nodes — a failure on a later type must not
        # leave the earlier types committed.
        by_type: dict[EdgeType, list[Edge]] = {}
        for e in edges:
            by_type.setdefault(e.type, []).append(e)
        self._conn.execute("BEGIN TRANSACTION")
        try:
            for edge_type, batch in by_type.items():
                self._insert_edge_batch(edge_type, batch)
            # COMMIT belongs inside the try: a failing COMMIT outside it would leave the
            # transaction open on this connection, and every later execute() on the store
            # would silently run inside that dangling transaction.
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._causes_cache = None
        self._trace_cache = None

    def ancestors(self, step_id: str) -> list[Step]:
        """Raw CAUSED_BY ancestors of ``step_id``, nearest-cause first.

        Implementation: pull single-hop CAUSED_BY adjacency for the whole graph, then BFS in
        Python. The BFS preserves the in-memory store's edge-order semantics on fan-in
        (Cypher gives no row-order guarantee, so we'd otherwise diverge on multi-parent
        steps). We deliberately do NOT use a variable-length match here: a whole-graph pull
        avoids backend-specific path bounds and guarantees complete RCA at any depth.
        """
        self._check_open()
        steps_by_id = {s.step_id: s for s in self._load_all_steps()}
        if step_id not in steps_by_id:
            raise KeyError(f"unknown step {step_id!r}")
        adjacency = self._cause_adjacency(steps_by_id)

        out: list[Step] = []
        seen: set[str] = {step_id}
        queue: deque[str] = deque(adjacency.get(step_id, []))
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            # Copy: the Step model is not frozen and these instances live in the shared
            # per-store cache — a caller mutating a result must not corrupt later reads.
            out.append(steps_by_id[cur].model_copy(deep=True))
            queue.extend(adjacency.get(cur, []))
        return out

    def export_artifact(self, path: str | Path) -> None:
        artifact.save_atomic(self.trace(), path)

    def trace(self) -> NormalizedTrace:
        """Reconstruct the loaded trace from the DB by re-running :func:`normalize`.

        We **only** pull the raw layer (steps + CAUSED_BY) from LadybugDB and let normalize()
        re-derive ``BELONGS_TO``/``TREE_PARENT``/``projection_lossy``. That collapses two
        round-trip invariants into one source of truth: LadybugStore is byte-stable iff
        ``normalize()`` is deterministic (it is). The TREE_PARENT and BELONGS_TO rows that
        live in the DB are still useful for ad-hoc Cypher against the derived layer; they
        just aren't load-bearing for artifact round-trip.
        """
        self._check_open()
        if self._trace is None:
            raise RuntimeError("store has no trace loaded")
        if self._trace_cache is not None:
            # Deep copy: the model is not frozen, so handing out the cached instance
            # would let one caller's mutation corrupt every later read (and the
            # pure-Python fallback matcher). A copy is still far cheaper than the
            # whole-graph dump + re-normalize it replaces.
            return self._trace_cache.model_copy(deep=True)
        raw_steps = [
            s.model_copy(update={"projection_lossy": False}) for s in self._load_all_steps()
        ]
        caused_by_pairs = _collect(
            self._conn.execute(
                "MATCH (a:Step)-[e:CAUSED_BY]->(b:Step) "
                "RETURN a.step_id, b.step_id, e.origin"
            )
        )
        raw_edges = [
            Edge(
                type=EdgeType.CAUSED_BY,
                src=src,
                dst=dst,
                origin=EdgeOrigin(origin) if origin else None,
            )
            for src, dst, origin in caused_by_pairs
        ]
        self._trace_cache = normalize(
            RawTrace(trace=self._trace, steps=raw_steps, causal_edges=raw_edges)
        )
        return self._trace_cache.model_copy(deep=True)

    # --- pattern matching (the reason LadybugDB earns its weight) ---

    def find_matches(self, pattern: PathPattern) -> list[list[str]]:
        """Run a :class:`PathPattern` as compiled Cypher.

        Returns the **same list, in the same order**, as
        :func:`tracegraph.analysis.find_matches` on the in-memory store. Pure-Python's
        traversal visits outer steps in canonical step order and expands each child level
        in canonical CAUSED_BY order — both of which, after :func:`normalize` canonicalization,
        sort to ``(seq, step_id)`` per position. So we reproduce that ordering on the rows
        Ladybug returns. (Cypher itself gives no row-order guarantee; without this sort the
        backends would silently disagree.)

        Honest fallback: a pattern with an unbounded (or over-30-hop) gap cannot be expressed
        within the compiler's verified variable-length cap, so ``compile_to_cypher`` raises
        :class:`~tracegraph.analysis.UncompilablePattern`. Rather than emit a query that would
        silently truncate, we fall back to a whole-graph pull (:meth:`trace`) plus the *same*
        pure-Python matcher — equivalence by identity, exactly the load-then-traverse posture
        :meth:`ancestors` already uses for the same cap. The accelerator degrades to *slower*,
        never to *wrong*; ``fell_back_to_python`` records that it happened.
        """
        self._check_open()
        self.fell_back_to_python = False
        if not pattern.steps:
            # Match the pure-Python convention so callers can be backend-agnostic; the
            # compile step would raise, but raising here would break that contract.
            return []
        try:
            q = compile_to_cypher(pattern)
        except UncompilablePattern:
            self.fell_back_to_python = True
            return py_find_matches(self.trace(), pattern)
        rows = _collect(self._conn.execute(q.cypher, q.params))
        steps_by_id = {s.step_id: s for s in self._load_all_steps()}
        rows.sort(key=lambda row: tuple((steps_by_id[sid].seq, sid) for sid in row))
        return [list(row) for row in rows]

    # --- internals ---

    def _insert_trace(self, t: Trace) -> None:
        placeholders = ", ".join(f"{f}: ${f}" for f in _TRACE_FIELDS)
        stmt = f"CREATE (:Trace {{{placeholders}}})"
        self._conn.execute(stmt, _trace_params(t))

    def _insert_edge_batch(self, edge_type: EdgeType, edges: list[Edge]) -> None:
        # BELONGS_TO targets a Trace node; the causal/tree edges stay Step→Step.
        target_label = "Trace" if edge_type is EdgeType.BELONGS_TO else "Step"
        relationship = (
            f"[:{edge_type.value} {{origin: r.origin}}]"
            if edge_type is EdgeType.CAUSED_BY
            else f"[:{edge_type.value}]"
        )
        stmt = (
            "UNWIND $rows AS r "
            f"MATCH (src:Step), (dst:{target_label}) "
            f"WHERE src.step_id = r.src AND dst.{'trace_id' if target_label == 'Trace' else 'step_id'} = r.dst "
            f"CREATE (src)-{relationship}->(dst) "
            "RETURN count(*)"
        )
        rows: list[dict[str, Any]] = []
        for e in edges:
            row: dict[str, Any] = {"src": e.src, "dst": e.dst}
            if edge_type is EdgeType.CAUSED_BY:
                row["origin"] = e.origin.value if e.origin else None
            rows.append(row)
        result = _collect(self._conn.execute(stmt, {"rows": rows}))
        created = result[0][0] if result else 0
        if created != len(rows):
            # UNWIND+MATCH silently skips a row whose endpoint doesn't exist; that would
            # break the batch's all-or-nothing contract, so surface it as an error — the
            # surrounding transaction in upsert_edges rolls the whole batch back.
            raise ValueError(
                f"{len(rows) - created} of {len(rows)} {edge_type.value} edge(s) "
                "reference unknown endpoints"
            )

    def _cause_adjacency(self, steps_by_id: dict[str, Step]) -> dict[str, list[str]]:
        """Whole-graph single-hop CAUSED_BY adjacency (effect -> cause), cached per store.

        Same load-then-traverse pattern as trace()/find_matches, reproducing
        InMemoryStore.ancestors exactly at any depth. Per-node cause order matches the
        InMemoryStore's CAUSED_BY insertion order, which after :func:`normalize`
        canonicalization is ``(effect.seq, cause.seq, src, dst)`` — Cypher gives no
        row-order guarantee, so we re-sort here.
        """
        if self._causes_cache is None:
            rows = _collect(
                self._conn.execute(
                    "MATCH (effect:Step)-[:CAUSED_BY]->(cause:Step) "
                    "RETURN effect.step_id, cause.step_id"
                )
            )
            adjacency: dict[str, list[str]] = {}
            for effect_id, cause_id in rows:
                adjacency.setdefault(effect_id, []).append(cause_id)
            for causes in adjacency.values():
                causes.sort(key=lambda cid: (steps_by_id[cid].seq, cid))
            self._causes_cache = adjacency
        return self._causes_cache

    def _load_all_steps(self) -> list[Step]:
        if self._steps_cache is None:
            rows = _collect(
                self._conn.execute(
                    "MATCH (s:Step) RETURN " + ", ".join(f"s.{f}" for f in _STEP_FIELDS)
                )
            )
            self._steps_cache = [_step_from_row(r) for r in rows]
        return self._steps_cache


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
        "evidence_json": (
            json.dumps(s.evidence.model_dump(mode="json"), sort_keys=True)
            if s.evidence is not None
            else None
        ),
        "projection_lossy": s.projection_lossy,
    }


def _trace_params(t: Trace) -> dict[str, Any]:
    return {
        "trace_id": t.trace_id,
        "source_kind": t.source_kind,
        "run_id": t.run_id,
        "thread_id": t.thread_id,
        "status": t.status.value,
        "causal_fidelity": t.causal_fidelity.value,
        "links_preserved": t.links_preserved,
        "decision_evidence_json": json.dumps(
            [item.model_dump(mode="json") for item in t.decision_evidence], sort_keys=True
        ),
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
        evidence=(
            StepEvidence.model_validate(json.loads(by_name["evidence_json"]))
            if by_name["evidence_json"]
            else None
        ),
        projection_lossy=by_name["projection_lossy"],
    )


def _collect(result: Any) -> list[list[Any]]:
    """Drain a Ladybug QueryResult into a plain list of rows, then release it.

    Every read path funnels through here, so leaving the result objects to the garbage
    collector accumulated one live handle per query for the lifetime of the store — visible
    on a directory sweep, where a single store answers many ancestors()/find_matches() calls.
    """
    rows: list[list[Any]] = []
    try:
        while result.has_next():
            rows.append(result.get_next())
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return rows
