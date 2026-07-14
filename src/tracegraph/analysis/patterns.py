"""Cross-trace pattern matching over the **raw** causal graph.

The headline "graph queries, not columns" capability: find a causal *sequence* of steps
(e.g. "a tool step that errored", "plan immediately followed by a failing tool") across many
traces at once. Reads the raw ``CAUSED_BY`` layer — patterns are about real causality, so they
must see every real predecessor, not the lossy single-parent projection.

A ``PathPattern`` is a backend-neutral spec: an ordered list of ``StepPredicate``. The matcher
here is pure-Python (zero optional deps); the *same spec* also compiles to standard Cypher
(see :func:`compile_to_cypher`), which is what the optional LadybugDB backend runs. The compiler is
deliberately kept here, next to the spec, so the spec is self-describing — "this is what a
``PathPattern`` means as a Cypher query" — and importable without the optional dependency.

Beyond a strictly-contiguous chain, a predicate can declare two cross-step relationships that
make the marquee "tool X → retry → tool X → failure" pattern expressible:

* ``gap`` — the predicate is reached from the previous one through a *variable-length* causal
  span (``(lo, hi)`` ``CAUSED_BY`` hops, ``hi=None`` meaning unbounded), not strict adjacency.
  This is the "→ retry →": some intervening steps, not necessarily one.
* ``same_name_as`` — a back-reference requiring this step's ``name`` to equal the name matched
  by an *earlier* predicate. This is the "the **same** tool X both times".

The pure-Python matcher is the **system of record** for every pattern. A bounded gap compiles
to a faithful ``CAUSED_BY*lo..hi`` Cypher query; an *unbounded* (or over-cap) gap cannot be
expressed within LadybugDB's 30-hop variable-length ceiling, so :func:`compile_to_cypher` raises
:class:`UncompilablePattern` rather than silently truncating — and the LadybugDB backend falls back
to a whole-graph pull plus this same matcher (see ``store/ladybug.py``). Nothing is ever silently
dropped.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from tracegraph.model import EdgeType, NormalizedTrace, Step, StepKind, StepStatus

#: LadybugDB 0.18.1 caps the upper bound of any variable-length relationship pattern
#: at 30 hops (``[:CAUSED_BY*2..1000000]`` raises "Upper bound of rel exceeds maximum: 30").
#: 30 is therefore the largest gap upper bound that compiles to *faithful* Cypher. This is
#: a verified backend limit, not an arbitrary choice — do NOT raise it without retesting the
#: supported LadybugDB range. See store/ladybug.py:ancestors and the
#: ``ladybug-varlength-hop-cap`` design note. Gaps larger than this (or unbounded) are matched by
#: the pure-Python system of record and refused by the compiler (UncompilablePattern).
MAX_GAP = 30


class UncompilablePattern(ValueError):
    """A pattern the pure-Python matcher accepts but Cypher cannot express faithfully.

    Raised by :func:`compile_to_cypher` for a gap that is unbounded or whose upper bound
    exceeds LadybugDB's 30-hop variable-length cap (:data:`MAX_GAP`). The backend must catch this
    and fall back to the pure-Python matcher (the system of record) rather than emit a query
    that silently truncates — so an uncompilable pattern degrades to *slower*, never to *wrong*.
    It subclasses ``ValueError`` so existing ``except ValueError`` callers stay correct.
    """


@dataclass(frozen=True)
class StepPredicate:
    """Matches a step on any combination of name / kind / status (unset fields are wildcards).

    Two optional fields express *cross-step* relationships (resolved by the matcher/compiler,
    never inside :meth:`matches`, which stays a pure single-step test):

    * ``gap`` — ``(lo, hi)`` variable-length ``CAUSED_BY`` hops connecting this predicate to the
      previous one instead of strict adjacency. ``lo``/``hi`` count *relationships*: ``(1, 1)``
      is strict adjacency (0 intervening steps), ``(2, …)`` means "≥1 intervening step". ``hi``
      may be ``None`` for an unbounded span. ``None`` (the default) is strict adjacency.
    * ``same_name_as`` — the index of an *earlier* predicate whose matched ``name`` this step's
      ``name`` must equal (a back-reference; both names must be non-null, mirroring Cypher's
      ``NULL = NULL`` → ``NULL`` semantics).
    """

    name: str | None = None
    kind: StepKind | None = None
    status: StepStatus | None = None
    same_name_as: int | None = None
    gap: tuple[int, int | None] | None = None

    def __post_init__(self) -> None:
        # Only single-field, index-free invariants here (a frozen dataclass __post_init__ can
        # read but not see sibling predicates). Cross-predicate checks live in _validate_backrefs.
        if self.gap is not None:
            lo, hi = self.gap
            if lo < 1:
                raise ValueError(f"gap lower bound must be >= 1, got {lo}")
            if hi is not None and hi < lo:
                raise ValueError(f"gap upper bound {hi} is below lower bound {lo}")
        if self.same_name_as is not None and self.same_name_as < 0:
            raise ValueError("same_name_as must be a non-negative predicate index")

    def matches(self, step: Step) -> bool:
        if self.name is not None and step.name != self.name:
            return False
        if self.kind is not None and step.kind is not self.kind:
            return False
        if self.status is not None and step.status is not self.status:
            return False
        return True

    def __str__(self) -> str:
        parts = []
        if self.name is not None:
            parts.append(f"name={self.name}")
        if self.kind is not None:
            parts.append(f"kind={self.kind.value}")
        if self.status is not None:
            parts.append(f"status={self.status.value}")
        if self.same_name_as is not None:
            parts.append(f"name=#{self.same_name_as}")  # "same name as predicate N"
        return "{" + ", ".join(parts) + "}" if parts else "{any}"


@dataclass(frozen=True)
class PathPattern:
    """A sequence of predicates matched against consecutive steps along a causal path.

    By default each predicate matches a *direct* causal child of the previous one (strict
    adjacency). A predicate carrying a ``gap`` is instead reached through a variable-length
    causal span — see :class:`StepPredicate`. The first predicate's ``gap`` is meaningless
    (nothing precedes it) and ignored.
    """

    steps: tuple[StepPredicate, ...]
    description: str = ""
    pattern_id: str | None = None
    pattern_version: int | None = None

    def __post_init__(self) -> None:
        if (self.pattern_id is None) != (self.pattern_version is None):
            raise ValueError("pattern_id and pattern_version must be set together")
        if self.pattern_version is not None and self.pattern_version <= 0:
            raise ValueError("pattern_version must be a positive integer")

    def __str__(self) -> str:
        if not self.steps:
            return ""
        out = [str(self.steps[0])]
        for p in self.steps[1:]:
            if p.gap is None:
                out.append(" → " + str(p))
            else:
                lo, hi = p.gap
                hi_s = "" if hi is None else str(hi)
                out.append(f" →[gap {lo}..{hi_s}]→ " + str(p))
        return "".join(out)


@dataclass
class Match:
    trace_id: str
    step_ids: list[str]
    labels: list[str] = field(default_factory=list)


def _children(nt: NormalizedTrace) -> dict[str, list[str]]:
    """cause_id -> [effect_id, ...] in execution order (CAUSED_BY points effect -> cause)."""
    out: dict[str, list[str]] = {}
    for e in nt.edges_of(EdgeType.CAUSED_BY):
        out.setdefault(e.dst, []).append(e.src)
    return out


def _validate_backrefs(preds: tuple[StepPredicate, ...]) -> None:
    """A ``same_name_as`` must reference a *strictly earlier* predicate.

    ``__post_init__`` can't see a predicate's own position, so the forward/self/out-of-range
    check lives here (called by both the matcher and the compiler). Raising loudly beats a
    silent no-match or a ``KeyError`` deep in traversal.
    """
    for i, p in enumerate(preds):
        if p.same_name_as is not None and p.same_name_as >= i:
            raise ValueError(
                f"predicate {i}'s same_name_as={p.same_name_as} must point to a strictly "
                f"earlier predicate (0..{i - 1})"
            )


def find_matches(nt: NormalizedTrace, pattern: PathPattern) -> list[list[str]]:
    """All causal paths whose steps satisfy the predicates in order.

    Strict-adjacency predicates advance to a direct causal child; a predicate with a ``gap``
    advances through a variable-length ``CAUSED_BY`` span. Back-references (``same_name_as``)
    constrain a step's ``name`` to a previously-matched one. Returns one step id per predicate
    (gap-interior steps are *not* included), so the match shape is independent of gap width.

    The result is deterministically ordered by ``(seq, step_id)`` per column and de-duplicated
    on the full path tuple — matching the LadybugDB backend's row sort and ``RETURN DISTINCT`` so the
    two backends agree on a fan-in graph where one endpoint is reachable by several gap paths.

    Performance: the headline ``tool-retry-failure`` shape (a tool, then an *unbounded* gap to a
    failing same-named tool) takes an endpoint-anchored fast path — see
    :func:`_match_two_step_unbounded`. A naive forward walk from every tool would be O(n²) on the
    deep-linear traces this project targets; the fast path is output-optimal. Every other shape
    uses :func:`_find_matches_forward`, which is linear for bounded/strict patterns (a bounded gap
    is capped at :data:`MAX_GAP` hops). The fast path returns *exactly* the same matches as the
    forward matcher (which remains the semantic reference); a randomized differential test pins
    that equality.
    """
    if not pattern.steps:
        return []
    _validate_backrefs(pattern.steps)
    preds = pattern.steps
    # Fast path for the marquee shape: [p0, p1 (unbounded gap)]. A gap on p0 is deliberately
    # ignored by the PathPattern contract because no predicate precedes it, so it must not disable
    # this optimization. The forward matcher is correct here too but O(n²) when many steps match
    # p0; the endpoint-anchored variant is O(n·k).
    if (
        len(preds) == 2
        and preds[1].gap is not None
        and preds[1].gap[1] is None
    ):
        return _match_two_step_unbounded(nt, preds[0], preds[1])
    return _find_matches_forward(nt, pattern)


def _name_ok(pred: StepPredicate, step: Step, bound: dict[int, str | None]) -> bool:
    """Resolve a ``same_name_as`` back-reference against the names bound by earlier predicates.

    Cypher's ``NULL = NULL`` is ``NULL`` (the row is dropped); mirror that — a nameless step can
    never satisfy a back-reference, on either side of the comparison — so the two backends agree.
    """
    if pred.same_name_as is None:
        return True
    ref = bound.get(pred.same_name_as)
    return ref is not None and step.name is not None and step.name == ref


def _find_matches_forward(nt: NormalizedTrace, pattern: PathPattern) -> list[list[str]]:
    """The general matcher: walk every predicate forward over the causal graph.

    This is the semantic reference for :func:`find_matches` — every other path must reproduce it
    exactly. Linear for strict/bounded patterns; only an *unbounded* gap (which the dispatcher
    routes elsewhere for the common 2-step shape) can make it quadratic.
    """
    steps = nt.steps_by_id()
    children = _children(nt)
    preds = pattern.steps
    results: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    # Recursion depth here == predicate index (len(preds), ~2-4), NOT trace depth — safe on the
    # deep-linear traces this project sees. The variable-length gap walk below is an explicit
    # deque (heap, not stack), so a 30-hop span over a 2000-step trace never grows the call stack.
    def advance(node_id: str, idx: int, acc: list[str], bound: dict[int, str | None]) -> None:
        step = steps[node_id]
        pred = preds[idx]
        if not pred.matches(step) or not _name_ok(pred, step, bound):
            return
        acc2 = acc + [node_id]
        if idx + 1 == len(preds):
            key = tuple(acc2)
            if key not in seen:  # == RETURN DISTINCT: collapse multiple gap paths to one row
                seen.add(key)
                results.append(acc2)
            return
        bound2 = {**bound, idx: step.name}
        nxt = preds[idx + 1]
        if nxt.gap is None:
            for child in children.get(node_id, []):
                advance(child, idx + 1, acc2, bound2)
            return
        lo, hi = nxt.gap
        unbounded = hi is None
        # BFS forward over CAUSED_BY (cause -> effect). De-dup on (node, distance) so a diamond
        # doesn't blow up exponentially while still visiting every distinct path *length* — an
        # endpoint reachable only at a longer-than-min distance within [lo, hi] is not missed.
        frontier: deque[tuple[str, int]] = deque((c, 1) for c in children.get(node_id, []))
        visited_states: set[tuple[str, int]] = set()
        while frontier:
            cur, dist = frontier.popleft()
            if (cur, dist) in visited_states:
                continue
            visited_states.add((cur, dist))
            if dist >= lo and (unbounded or dist <= hi):
                advance(cur, idx + 1, acc2, bound2)
            if unbounded or dist < hi:
                for c in children.get(cur, []):
                    frontier.append((c, dist + 1))

    for sid in steps:
        advance(sid, 0, [], {})
    results.sort(key=lambda row: tuple((steps[sid].seq, sid) for sid in row))
    return results


def _match_two_step_unbounded(
    nt: NormalizedTrace, p0: StepPredicate, p1: StepPredicate
) -> list[list[str]]:
    """Endpoint-anchored matcher for ``[p0, p1-with-unbounded-gap]`` (the marquee shape).

    A match is ``(a, e)`` where ``a`` satisfies ``p0``, ``e`` satisfies ``p1`` (incl. its
    back-reference to ``a``), and ``e`` is reachable from ``a`` by a causal path of length
    ``>= lo`` (the gap's lower bound; the upper bound is unbounded). "A path of length ``>= lo``
    exists" is exactly "the *longest* path ``a -> e`` is ``>= lo``", so we work backward from each
    ``p1`` endpoint, computing the longest causal distance to it for every ancestor in one
    reverse-topological pass. Cost is ``O(|endpoints| * (n + edges))`` — output-optimal, and
    ``O(n)`` for the realistic case where failing tools are rare. (A forward walk from every
    ``p0`` match would be ``O(n²)`` on a deep-linear trace; this avoids re-traversing the shared
    downstream suffix.) Produces exactly what :func:`_find_matches_forward` would.
    """
    steps = nt.steps_by_id()
    children = _children(nt)  # cause -> [effect, ...]
    parents: dict[str, list[str]] = {}  # effect -> [cause, ...]
    for cause, effects in children.items():
        for eff in effects:
            parents.setdefault(eff, []).append(cause)

    lo = p1.gap[0]  # type: ignore[index]  (dispatcher guarantees p1.gap is (lo, None))
    results: list[list[str]] = []
    for e in (sid for sid, st in steps.items() if p1.matches(st)):
        # Ancestors of e that p0 could match (backward reachability over CAUSED_BY).
        ancestors: set[str] = set()
        stack = [e]
        while stack:
            node = stack.pop()
            for cause in parents.get(node, []):
                if cause not in ancestors:
                    ancestors.add(cause)
                    stack.append(cause)
        # Longest causal distance to e for each ancestor: process effects before causes (seq
        # descending), so a node's children already have their longest-to-e when we reach it.
        longest: dict[str, int] = {e: 0}
        for a in sorted(ancestors, key=lambda nid: steps[nid].seq, reverse=True):
            best = max(
                (longest[c] + 1 for c in children.get(a, []) if c in longest),
                default=0,
            )
            if best:
                longest[a] = best
        ename = steps[e].name
        for a in ancestors:
            if longest.get(a, 0) < lo or not p0.matches(steps[a]):
                continue
            # Back-reference (p1.same_name_as can only be 0 == p0 here): names must match, both
            # non-null — same NULL semantics as _name_ok / the compiled `IS NOT NULL` guard.
            if p1.same_name_as is not None and not (
                ename is not None and steps[a].name is not None and ename == steps[a].name
            ):
                continue
            results.append([a, e])
    results.sort(key=lambda row: tuple((steps[sid].seq, sid) for sid in row))
    return results


def search(traces: list[NormalizedTrace], pattern: PathPattern) -> list[Match]:
    """Run a pattern over many traces; return every match with readable step labels."""
    matches: list[Match] = []
    for nt in traces:
        steps = nt.steps_by_id()
        for path in find_matches(nt, pattern):
            labels = [steps[i].name or steps[i].kind.value for i in path]
            matches.append(Match(trace_id=nt.trace.trace_id, step_ids=path, labels=labels))
    return matches


@dataclass(frozen=True)
class CompiledQuery:
    """A Cypher query equivalent to a :class:`PathPattern`.

    ``cypher`` uses parameter placeholders (``$name``) — never string-interpolated user data —
    so the same compiled artifact is safe to run against any Cypher engine without
    rebuilding per call. ``result_vars`` lists the RETURN column aliases in pattern order:
    each row's columns are the matched step ids for predicate 0, 1, … in turn.
    """

    cypher: str
    params: dict[str, str]
    result_vars: tuple[str, ...]


def _where_clauses(idx: int, pred: StepPredicate, params: dict[str, str]) -> list[str]:
    """Translate a predicate into ``s{idx}.<field> = $param`` clauses, recording the params.

    Only fields the predicate actually constrains become clauses (mirroring the pure-Python
    matcher's "unset = wildcard" rule). Enum values are stored as their ``.value`` strings,
    matching how LadybugDB (and any other backend) serializes a step row. A ``same_name_as`` adds a
    column-to-column equality (not a param) plus a NOT-NULL guard so the backends agree on
    nameless steps (Cypher ``NULL = NULL`` is ``NULL`` → row dropped, matching the matcher).
    """
    clauses: list[str] = []
    if pred.name is not None:
        key = f"s{idx}_name"
        params[key] = pred.name
        clauses.append(f"s{idx}.name = ${key}")
    if pred.kind is not None:
        key = f"s{idx}_kind"
        params[key] = pred.kind.value
        clauses.append(f"s{idx}.kind = ${key}")
    if pred.status is not None:
        key = f"s{idx}_status"
        params[key] = pred.status.value
        clauses.append(f"s{idx}.status = ${key}")
    if pred.same_name_as is not None:
        clauses.append(f"s{idx}.name IS NOT NULL")
        clauses.append(f"s{idx}.name = s{pred.same_name_as}.name")
    return clauses


def _rel(pred: StepPredicate) -> str:
    """The compiled relationship reaching ``pred`` from the previous node.

    Strict adjacency is a single backward ``CAUSED_BY`` hop (byte-identical to the pre-gap
    output). A bounded gap becomes a variable-length ``*lo..hi`` span. An unbounded or
    over-cap gap can't be expressed within LadybugDB's 30-hop ceiling, so we refuse rather than
    emit a query that silently truncates.
    """
    if pred.gap is None:
        return "<-[:CAUSED_BY]-"
    lo, hi = pred.gap
    if hi is None:
        raise UncompilablePattern(
            f"unbounded gap (lo={lo}) cannot be compiled within LadybugDB's {MAX_GAP}-hop "
            "variable-length cap; the pure-Python matcher is the system of record for it"
        )
    if hi > MAX_GAP:
        raise UncompilablePattern(
            f"gap upper bound {hi} exceeds LadybugDB's {MAX_GAP}-hop variable-length cap; "
            "the pure-Python matcher is the system of record for it"
        )
    return f"<-[:CAUSED_BY*{lo}..{hi}]-"


def compile_to_cypher(pattern: PathPattern) -> CompiledQuery:
    """Compile a :class:`PathPattern` to a parameterized Cypher query.

    Edge direction note: the model stores ``CAUSED_BY`` as effect → cause, but a
    ``PathPattern`` reads cause → effect (predicate 0 is matched first, predicate 1 is its
    causal effect, etc). The compiled MATCH therefore walks the relationship *backwards*
    (``s0 <-[:CAUSED_BY]- s1``), so the row order matches the pure-Python matcher's
    ``find_matches`` exactly.

    A predicate carrying a ``gap`` compiles to a variable-length span; because a var-length
    match yields one row *per path*, any gap forces ``RETURN DISTINCT`` so a fan-in graph
    collapses to one row per endpoint tuple — exactly what the pure-Python matcher's de-dup
    produces. A ``same_name_as`` compiles to a column-equality back-reference.

    Raises :class:`UncompilablePattern` (a ``ValueError`` subclass) when a gap is unbounded or
    exceeds :data:`MAX_GAP` — the caller (the LadybugDB store) must then fall back to the pure-Python
    matcher rather than run a query that would silently truncate. Raises plain ``ValueError`` on
    an empty pattern — that's a caller bug, not a query that returns nothing.
    """
    if not pattern.steps:
        raise ValueError("cannot compile an empty PathPattern")
    _validate_backrefs(pattern.steps)

    n = len(pattern.steps)
    nodes = [f"(s{i}:Step)" for i in range(n)]
    # cause → effect along the pattern == effect → cause along CAUSED_BY, reversed in MATCH.
    match = nodes[0] + "".join(_rel(pattern.steps[i]) + nodes[i] for i in range(1, n))

    params: dict[str, str] = {}
    clauses: list[str] = []
    for i, pred in enumerate(pattern.steps):
        clauses.extend(_where_clauses(i, pred, params))

    # A variable-length match returns one row per path; DISTINCT collapses those to one row per
    # endpoint tuple, matching the pure-Python matcher's de-dup. Strict-only patterns can't
    # produce duplicate tuples (no repeated CAUSED_BY edges), so they keep the bare RETURN —
    # preserving the exact compiled string for the existing presets.
    # A gap belongs to the relationship reaching its predicate. Predicate 0 has no incoming
    # relationship, so its gap is contractually ignored and must not add a spurious DISTINCT.
    has_gap = any(p.gap is not None for p in pattern.steps[1:])
    ret = "RETURN DISTINCT " if has_gap else "RETURN "

    parts = [f"MATCH {match}"]
    if clauses:
        parts.append("WHERE " + " AND ".join(clauses))
    parts.append(ret + ", ".join(f"s{i}.step_id AS s{i}" for i in range(n)))
    return CompiledQuery(
        cypher="\n".join(parts),
        params=params,
        result_vars=tuple(f"s{i}" for i in range(n)),
    )


#: Named preset patterns shipped with the CLI.
PRESETS: dict[str, PathPattern] = {
    "error": PathPattern(
        (StepPredicate(status=StepStatus.ERROR),),
        "any step that errored",
        pattern_id="error",
        pattern_version=1,
    ),
    "tool-failure": PathPattern(
        (StepPredicate(kind=StepKind.TOOL, status=StepStatus.ERROR),),
        "a tool step that errored",
        pattern_id="tool-failure",
        pattern_version=1,
    ),
    "plan-then-tool-failure": PathPattern(
        (
            StepPredicate(name="plan"),
            StepPredicate(kind=StepKind.TOOL, status=StepStatus.ERROR),
        ),
        "a 'plan' step immediately followed (causally) by a failing tool",
        pattern_id="plan-then-tool-failure",
        pattern_version=1,
    ),
    # The marquee cross-trace pattern advertised in the README / FEASIBILITY:
    # "tool X → retry → tool X → failure". Predicate 0 binds the first tool's name; predicate 1
    # requires the SAME name (same_name_as=0), a failing status, and an *unbounded* causal gap
    # (≥1 intervening step — the retry machinery). The gap is unbounded because real traces are
    # deep-linear and a retry can be many super-steps later; the pure-Python matcher catches it
    # at any distance, and the LadybugDB backend transparently falls back to it (the compiled form is
    # uncompilable past 30 hops). See `tool-retry-failure-near` for a Cypher-acceleratable bound.
    "tool-retry-failure": PathPattern(
        (
            StepPredicate(kind=StepKind.TOOL),
            StepPredicate(
                kind=StepKind.TOOL,
                status=StepStatus.ERROR,
                same_name_as=0,
                gap=(2, None),
            ),
        ),
        "the same tool called again (after a retry) and failing — "
        "'tool X → retry → tool X → failure' (any causal distance)",
        pattern_id="tool-retry-failure",
        pattern_version=1,
    ),
    # Bounded variant of the marquee: the failing retry within MAX_GAP causal hops of the first
    # call. Identical matches to `tool-retry-failure` for nearby retries, but it compiles to a
    # faithful `CAUSED_BY*2..30` query so the LadybugDB accelerator runs it natively.
    "tool-retry-failure-near": PathPattern(
        (
            StepPredicate(kind=StepKind.TOOL),
            StepPredicate(
                kind=StepKind.TOOL,
                status=StepStatus.ERROR,
                same_name_as=0,
                gap=(2, MAX_GAP),
            ),
        ),
        f"the same tool retried within {MAX_GAP} causal hops and failing "
        "(Cypher-acceleratable form of tool-retry-failure)",
        pattern_id="tool-retry-failure-near",
        pattern_version=1,
    ),
}

_mismatched_preset_ids = [
    (name, pattern.pattern_id)
    for name, pattern in PRESETS.items()
    if name != pattern.pattern_id
]
if _mismatched_preset_ids:
    raise RuntimeError(f"PRESETS keys must equal pattern_id: {_mismatched_preset_ids!r}")
