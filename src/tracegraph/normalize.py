"""The correctness core: validate the raw causal graph, then derive the tree.

Adapters emit a :class:`~tracegraph.model.RawTrace` carrying steps and the **raw**
``CAUSED_BY`` edges (every real predecessor of every step). :func:`normalize`:

* **validates** the raw graph (:func:`validate_raw`) — endpoints exist, edges are
  ``CAUSED_BY``, no self-edges, and every cause precedes its effect (which also makes
  the raw graph acyclic by construction),
* derives the ``TREE_PARENT`` layer — exactly one parent per non-root step — by prioritizing
  explicit structural graph parents over containment and links, breaking ties by earliest time
  (with legacy temporal-only fallback for backward compatibility),
* flags ``projection_lossy=True`` on any step whose real causality had to be collapsed
  (more than one ``CAUSED_BY``), and
* re-checks the derived layer is a forest (:func:`validate_tree`).

The raw layer is never mutated. This is the boundary Codex flagged: keep the lossy
projection *explicit and inspectable* so root-cause analysis (which reads the raw layer)
never silently trusts a fabricated single parent — and reject malformed input here rather
than letting it become a malformed artifact downstream.
"""

from __future__ import annotations

from tracegraph.model import (
    Edge,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepStatus,
)


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
    """Validate the raw CAUSED_BY layer: typed, endpoints known/distinct, cause precedes effect.

    Also rejects duplicate edges — a repeated ``(src, dst)`` would double-count in pattern
    matches and child traversals.
    """
    seen: set[tuple[str, str]] = set()
    for e in edges:
        if e.type is not EdgeType.CAUSED_BY:
            raise ValueError(f"raw causal edges must all be CAUSED_BY, got {e.type.value}")
        if e.src not in steps:
            raise ValueError(f"CAUSED_BY src {e.src!r} is not a known step")
        if e.dst not in steps:
            raise ValueError(f"CAUSED_BY dst {e.dst!r} is not a known step")
        if e.src == e.dst:
            raise ValueError(f"self-causal edge on step {e.src!r}")
        if (e.src, e.dst) in seen:
            raise ValueError(f"duplicate CAUSED_BY edge {e.src!r} -> {e.dst!r}")
        seen.add((e.src, e.dst))
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


def validate_structure(nt: NormalizedTrace) -> None:
    """Assert a :class:`NormalizedTrace` is structurally sound, raising ``ValueError``.

    The subset of :func:`validate_normalized` that every *reader* depends on: unique step ids,
    a well-formed raw ``CAUSED_BY`` layer (known endpoints, cause precedes effect) and a
    ``TREE_PARENT`` forest. Analyses index steps by id and rely on ``seq`` ordering to walk
    causes in one pass, so on a malformed trace they would otherwise fail with a raw
    ``KeyError`` — neither ``OSError`` nor ``ValueError``, and therefore straight past every
    load-boundary handler. This is the cheap check a library entry point can afford; it
    deliberately does *not* re-derive the artifact, so a caller holding a consistent but
    non-canonical trace (an appended step, say) can still analyze it.
    """
    _check_steps(nt.trace.trace_id, nt.steps)
    _check_caused_by(nt.steps_by_id(), nt.edges_of(EdgeType.CAUSED_BY))
    validate_tree(nt)


def validate_normalized(nt: NormalizedTrace) -> None:
    """Validate a :class:`NormalizedTrace` end to end: raw layer **and** that the whole
    artifact is *exactly* what :func:`normalize` would produce from its raw layer.

    The strong check — re-deriving and comparing — is what makes the artifact a true
    system of record. Without it, a hand-edited or partly-corrupt artifact (missing
    ``BELONGS_TO``, missing ``TREE_PARENT``, wrong ``projection_lossy``, or simply edges
    in non-canonical order) would pass the cheap structural checks and then silently
    drift: a backend rebuilding from the raw layer would emit different artifact bytes
    on the next save. Rejecting here keeps every loader (InMemoryStore, LadybugStore, future
    caches) honest about "the JSON is what normalize() produces, full stop."
    """
    validate_structure(nt)

    # The trace header is part of the system of record: a stored status that disagrees with
    # the steps would let a "clean" artifact hide a failed run. normalize() derives it, so an
    # honest artifact always matches — we check it explicitly here for a clear error message
    # (the re-derive below would also catch it, but only as a generic "not canonical").
    any_error = any(s.status is StepStatus.ERROR for s in nt.steps)
    expected_status = StepStatus.ERROR if any_error else StepStatus.OK
    if nt.trace.status is not expected_status:
        raise ValueError(
            f"trace.status {nt.trace.status.value!r} disagrees with its steps (expected "
            f"{expected_status.value!r}: {'a' if any_error else 'no'} step has status=error)"
        )

    # Reconstruct what the canonical form should look like from the raw layer alone. We
    # strip projection_lossy from the steps so normalize() resets it from scratch —
    # otherwise a corrupt input flag would survive into the "expected" side and the
    # check would tautologically pass.
    raw = RawTrace(
        trace=nt.trace,
        steps=[s.model_copy(update={"projection_lossy": False}) for s in nt.steps],
        causal_edges=nt.edges_of(EdgeType.CAUSED_BY),
    )
    expected = normalize(raw, origin_priority=True)
    if expected != nt:
        # Backward compatibility: accept authentic legacy artifacts produced under
        # the previous temporal-only parent selection rule.
        legacy_expected = normalize(raw, origin_priority=False)
        if legacy_expected != nt:
            raise ValueError(
                "normalized trace is not in canonical form: derived edges, projection_lossy "
                "flags, or step/edge ordering do not match what normalize() produces from "
                "the raw CAUSED_BY layer. The JSON artifact must be the output of normalize() "
                "verbatim — anything else would drift on the next save."
            )


_ORIGIN_PRIORITY: dict[EdgeOrigin | None, int] = {
    EdgeOrigin.CHECKPOINT_PARENT: 0,
    EdgeOrigin.GRAPH_PARENT: 0,
    EdgeOrigin.SPAN_PARENT_FALLBACK: 1,
    EdgeOrigin.SPAN_LINK: 2,
    EdgeOrigin.LEGACY_UNKNOWN: 3,
    None: 3,
}


def _incoming_edges_by_step(caused_by: list[Edge]) -> dict[str, list[Edge]]:
    """Raw causes per effect step (CAUSED_BY points effect → cause), built in one pass.

    Preserves the order of ``caused_by``, so feeding it the canonically sorted edge
    list yields each step's incoming causal edges in canonical order.
    """
    incoming: dict[str, list[Edge]] = {}
    for e in caused_by:
        incoming.setdefault(e.src, []).append(e)
    return incoming


def _parents_by_step(caused_by: list[Edge]) -> dict[str, list[str]]:
    """Convenience helper returning destination step IDs per effect step."""
    return {src: [e.dst for e in edges] for src, edges in _incoming_edges_by_step(caused_by).items()}


def _primary_parent(
    incoming_edges: list[Edge],
    steps: dict[str, Step],
    *,
    origin_priority: bool = True,
) -> str:
    """Pick the single TREE_PARENT.

    When ``origin_priority=True`` (default), explicit framework/graph execution parents
    (CHECKPOINT_PARENT, GRAPH_PARENT) outrank span containment (SPAN_PARENT_FALLBACK),
    which outranks non-hierarchical causal links (SPAN_LINK), which outranks unrecorded/legacy
    origins (LEGACY_UNKNOWN, None).

    When ``origin_priority=False`` (legacy compatibility mode), parent choice is purely
    temporally-earliest cause.

    In both modes, ties are broken by earliest cause ordered by ``(seq, ts, step_id)``.
    """

    def key(edge: Edge) -> tuple[int, int, str, str]:
        p = steps[edge.dst]
        prio = _ORIGIN_PRIORITY.get(edge.origin, 3) if origin_priority else 0
        return (prio, p.seq, p.ts or "", p.step_id)

    chosen_edge = min(incoming_edges, key=key)
    return chosen_edge.dst


def normalize(raw: RawTrace, *, origin_priority: bool = True) -> NormalizedTrace:
    """Validate a raw trace and return its normalized form (raw + derived layers).

    Deterministic and **canonically ordered**: the returned trace's steps and edges are
    in a fixed, input-independent order so two semantically-equivalent ``RawTrace`` inputs
    (e.g. the same trace from two adapters, or the same artifact reloaded through any
    backend) produce byte-identical artifacts. The canonical layout is:

    * **steps** — sorted by ``(seq, step_id)``;
    * **CAUSED_BY** edges — sorted by ``(effect.seq, cause.seq, src, dst)`` (the order a
      walk-forward-in-time would discover them);
    * **BELONGS_TO** then **TREE_PARENT** for each step, emitted in the canonical step order.

    This guarantee — not "the adapter happened to enumerate canonically" — is what makes
    the optional LadybugDB cache a true accelerator: any backend that rebuilds a trace from
    its raw layer lands on the same artifact bytes.
    """
    validate_raw(raw)
    steps_by_id = raw.steps_by_id()

    # Canonicalize the raw inputs ONCE; derived edges then fall out in canonical order
    # naturally because they're emitted per-step in the same loop.
    steps_canonical = sorted(raw.steps, key=lambda s: (s.seq, s.step_id))
    caused_by = sorted(
        raw.causal_edges,
        key=lambda e: (steps_by_id[e.src].seq, steps_by_id[e.dst].seq, e.src, e.dst),
    )

    edges: list[Edge] = list(caused_by)  # carry the raw layer through unchanged
    incoming_by_step = _incoming_edges_by_step(caused_by)
    new_steps: list[Step] = []
    for step in steps_canonical:
        incoming = incoming_by_step.get(step.step_id, [])
        # Flag lossiness on a copy so we never mutate the caller's objects.
        step = step.model_copy(update={"projection_lossy": len(incoming) > 1})
        new_steps.append(step)

        edges.append(Edge(type=EdgeType.BELONGS_TO, src=step.step_id, dst=raw.trace.trace_id))
        if incoming:
            chosen = _primary_parent(incoming, steps_by_id, origin_priority=origin_priority)
            edges.append(Edge(type=EdgeType.TREE_PARENT, src=step.step_id, dst=chosen))

    # The trace-level status is a derived view of its steps, not independent data: make it
    # canonical here so the artifact header can never lie about whether the run errored.
    any_error = any(s.status is StepStatus.ERROR for s in new_steps)
    trace = raw.trace.model_copy(
        update={"status": StepStatus.ERROR if any_error else StepStatus.OK}
    )
    nt = NormalizedTrace(trace=trace, steps=new_steps, edges=edges)
    validate_tree(nt)
    return nt


def validate_tree(nt: NormalizedTrace) -> None:
    """Assert the **derived** ``TREE_PARENT`` layer is a forest. Never touches the raw layer.

    Raises ``ValueError`` if any step has more than one ``TREE_PARENT``, an edge
    references an unknown step, or following parents reveals a cycle.
    """
    ids = {s.step_id for s in nt.steps}
    seq_of = {s.step_id: s.seq for s in nt.steps}
    parent_of: dict[str, str] = {}
    for e in nt.edges_of(EdgeType.TREE_PARENT):
        if e.src not in ids or e.dst not in ids:
            raise ValueError(f"TREE_PARENT edge references unknown step: {e.src} -> {e.dst}")
        if e.src in parent_of:
            raise ValueError(f"step {e.src!r} has more than one TREE_PARENT (not a forest)")
        parent_of[e.src] = e.dst

    # Cycle check: walking parents from any node must terminate at a root. Nodes on an
    # already-proven-acyclic path are skipped, keeping the whole check O(n) overall.
    proven: set[str] = set()
    # Iterate steps in canonical (seq, step_id) order, not set order: which member of a cycle
    # the error names would otherwise vary with PYTHONHASHSEED, making the message — and any
    # test or bug report quoting it — irreproducible. The raise/no-raise decision is unaffected.
    for start in sorted(ids, key=lambda sid: (seq_of[sid], sid)):
        path: list[str] = []
        on_path: set[str] = set()
        cur: str | None = start
        while cur is not None and cur not in proven:
            if cur in on_path:
                raise ValueError(f"cycle in TREE_PARENT layer involving step {cur!r}")
            path.append(cur)
            on_path.add(cur)
            cur = parent_of.get(cur)
        proven.update(path)


def requires_origin_priority_projection(nt: NormalizedTrace) -> bool:
    """True iff this trace's TREE_PARENT projection differs from legacy temporal-only selection."""
    try:
        raw = RawTrace(
            trace=nt.trace,
            steps=[s.model_copy(update={"projection_lossy": False}) for s in nt.steps],
            causal_edges=nt.edges_of(EdgeType.CAUSED_BY),
        )
        return nt != normalize(raw, origin_priority=False)
    except ValueError:
        # If the trace is malformed, do not fail during serialization stamping.
        # Store loading and validate_normalized remain the validation gate.
        return False
