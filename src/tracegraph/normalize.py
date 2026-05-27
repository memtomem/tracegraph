"""The correctness core: validate the raw causal graph, then derive the tree.

Adapters emit a :class:`~tracegraph.model.RawTrace` carrying steps and the **raw**
``CAUSED_BY`` edges (every real predecessor of every step). :func:`normalize`:

* **validates** the raw graph (:func:`validate_raw`) — endpoints exist, edges are
  ``CAUSED_BY``, no self-edges, and every cause precedes its effect (which also makes
  the raw graph acyclic by construction),
* derives the ``TREE_PARENT`` layer — exactly one parent per non-root step — by picking
  each step's *temporally-earliest* cause,
* flags ``projection_lossy=True`` on any step whose real causality had to be collapsed
  (more than one ``CAUSED_BY``), and
* re-checks the derived layer is a forest (:func:`validate_tree`).

The raw layer is never mutated. This is the boundary Codex flagged: keep the lossy
projection *explicit and inspectable* so root-cause analysis (which reads the raw layer)
never silently trusts a fabricated single parent — and reject malformed input here rather
than letting it become a malformed artifact downstream.
"""

from __future__ import annotations

from tracegraph.model import Edge, EdgeType, NormalizedTrace, RawTrace, Step


def _check_steps(trace_id: str, steps: list[Step]) -> None:
    ids = [s.step_id for s in steps]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate step_id in trace")
    for s in steps:
        if s.trace_id != trace_id:
            raise ValueError(
                f"step {s.step_id!r} has trace_id {s.trace_id!r}, expected {trace_id!r}"
            )


def _check_caused_by(steps: dict[str, Step], edges: list[Edge]) -> None:
    """Validate the raw CAUSED_BY layer: typed, endpoints known/distinct, cause precedes effect."""
    for e in edges:
        if e.type is not EdgeType.CAUSED_BY:
            raise ValueError(f"raw causal edges must all be CAUSED_BY, got {e.type.value}")
        if e.src not in steps:
            raise ValueError(f"CAUSED_BY src {e.src!r} is not a known step")
        if e.dst not in steps:
            raise ValueError(f"CAUSED_BY dst {e.dst!r} is not a known step")
        if e.src == e.dst:
            raise ValueError(f"self-causal edge on step {e.src!r}")
        if not steps[e.dst].seq < steps[e.src].seq:
            raise ValueError(
                f"cause {e.dst!r} (seq {steps[e.dst].seq}) does not precede effect "
                f"{e.src!r} (seq {steps[e.src].seq})"
            )


def validate_raw(raw: RawTrace) -> None:
    """Assert a :class:`RawTrace` is well-formed before projection. Raises ``ValueError``.

    Checks: unique step ids; steps belong to the trace; every edge is ``CAUSED_BY`` with
    both endpoints known and distinct; and each cause's ``seq`` strictly precedes its
    effect's. The seq rule both encodes "a cause happens before its effect" and guarantees
    the raw graph has no cycles (so traversal always terminates).
    """
    _check_steps(raw.trace.trace_id, raw.steps)
    _check_caused_by(raw.steps_by_id(), raw.causal_edges)


def validate_normalized(nt: NormalizedTrace) -> None:
    """Validate a :class:`NormalizedTrace` end to end: raw ``CAUSED_BY`` layer **and** tree.

    Because the artifact JSON is the system of record, a hand-edited or corrupt artifact
    must be rejected at the load boundary — not silently accepted and then blow up inside
    ``explain``/``ancestors``. This re-runs the raw checks (on the ``CAUSED_BY`` subset of
    the edges) plus :func:`validate_tree`.
    """
    _check_steps(nt.trace.trace_id, nt.steps)
    _check_caused_by(nt.steps_by_id(), nt.edges_of(EdgeType.CAUSED_BY))
    validate_tree(nt)


def _parents_of(step_id: str, caused_by: list[Edge]) -> list[str]:
    """Raw causes of ``step_id`` (CAUSED_BY points effect → cause)."""
    return [e.dst for e in caused_by if e.src == step_id]


def _primary_parent(parent_ids: list[str], steps: dict[str, Step]) -> str:
    """Pick the single TREE_PARENT: the temporally-earliest cause.

    Ordering key is ``(seq, ts, step_id)`` so the choice is deterministic even when
    seq or ts collide. The chosen parent is the one that happened first.
    """

    def key(pid: str) -> tuple[int, str, str]:
        p = steps[pid]
        return (p.seq, p.ts or "", p.step_id)

    return min(parent_ids, key=key)


def normalize(raw: RawTrace) -> NormalizedTrace:
    """Validate a raw trace and return its normalized form (raw + derived layers).

    Deterministic. The returned :class:`NormalizedTrace` carries the original
    ``CAUSED_BY`` edges (system of record), plus derived ``BELONGS_TO`` and
    ``TREE_PARENT`` edges, with ``projection_lossy`` set on collapsed steps.
    """
    validate_raw(raw)
    steps_by_id = raw.steps_by_id()
    caused_by = raw.causal_edges

    edges: list[Edge] = list(caused_by)  # carry the raw layer through unchanged
    new_steps: list[Step] = []
    for step in raw.steps:
        parents = _parents_of(step.step_id, caused_by)
        # Flag lossiness on a copy so we never mutate the caller's objects.
        step = step.model_copy(update={"projection_lossy": len(parents) > 1})
        new_steps.append(step)

        edges.append(Edge(type=EdgeType.BELONGS_TO, src=step.step_id, dst=raw.trace.trace_id))
        if parents:
            chosen = _primary_parent(parents, steps_by_id)
            edges.append(Edge(type=EdgeType.TREE_PARENT, src=step.step_id, dst=chosen))

    nt = NormalizedTrace(trace=raw.trace, steps=new_steps, edges=edges)
    validate_tree(nt)
    return nt


def validate_tree(nt: NormalizedTrace) -> None:
    """Assert the **derived** ``TREE_PARENT`` layer is a forest. Never touches the raw layer.

    Raises ``ValueError`` if any step has more than one ``TREE_PARENT``, an edge
    references an unknown step, or following parents reveals a cycle.
    """
    ids = {s.step_id for s in nt.steps}
    parent_of: dict[str, str] = {}
    for e in nt.edges_of(EdgeType.TREE_PARENT):
        if e.src not in ids or e.dst not in ids:
            raise ValueError(f"TREE_PARENT edge references unknown step: {e.src} -> {e.dst}")
        if e.src in parent_of:
            raise ValueError(f"step {e.src!r} has more than one TREE_PARENT (not a forest)")
        parent_of[e.src] = e.dst

    # Cycle check: walking parents from any node must terminate at a root.
    for start in ids:
        seen: set[str] = set()
        cur: str | None = start
        while cur is not None:
            if cur in seen:
                raise ValueError(f"cycle in TREE_PARENT layer involving step {cur!r}")
            seen.add(cur)
            cur = parent_of.get(cur)
