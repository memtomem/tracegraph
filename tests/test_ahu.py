"""Rooted-tree (AHU) isomorphism + structural diff."""

from tracegraph.analysis import diff, is_isomorphic, structure_only
from tracegraph.model import Edge, EdgeType, RawTrace, Step, Trace
from tracegraph.normalize import normalize


def _chain(*names: str):
    """A linear chain names[0](root) -> names[1] -> ... as a normalized trace."""
    steps = [Step(step_id=f"s{i}", trace_id="t", seq=i, name=n) for i, n in enumerate(names)]
    edges = [
        Edge(type=EdgeType.CAUSED_BY, src=f"s{i}", dst=f"s{i - 1}")
        for i in range(1, len(names))
    ]
    return normalize(RawTrace(trace=Trace(trace_id="t", source_kind="x"), steps=steps, causal_edges=edges))


def _fanout(root: str, *leaves: str):
    """root with `leaves` as direct children (a star)."""
    steps = [Step(step_id="r", trace_id="t", seq=0, name=root)]
    edges = []
    for i, leaf in enumerate(leaves, start=1):
        steps.append(Step(step_id=f"l{i}", trace_id="t", seq=i, name=leaf))
        edges.append(Edge(type=EdgeType.CAUSED_BY, src=f"l{i}", dst="r"))
    return normalize(RawTrace(trace=Trace(trace_id="t", source_kind="x"), steps=steps, causal_edges=edges))


def test_identical_chains_are_isomorphic():
    a, b = _chain("a", "b", "c"), _chain("a", "b", "c")
    assert is_isomorphic(a, b)
    assert diff(a, b).identical


def test_relabel_is_detected():
    result = diff(_chain("a", "b", "c"), _chain("a", "X", "c"))
    assert not result.identical
    assert any("A='b'" in c and "B='X'" in c for c in result.changes)


def test_inserted_node_is_pinpointed():
    # A has an extra trailing node 'd'
    result = diff(_chain("a", "b", "c", "d"), _chain("a", "b", "c"))
    assert not result.identical
    assert any("only in A" in c and "subtree d" in c for c in result.changes)


def test_child_order_does_not_matter():
    # same multiset of children, different insertion order -> isomorphic (AHU sorts kids)
    assert is_isomorphic(_fanout("r", "x", "y"), _fanout("r", "y", "x"))


def test_structure_only_ignores_labels():
    # different names, identical shape
    a, b = _chain("a", "b", "c"), _chain("p", "q", "r")
    assert not is_isomorphic(a, b)  # labelled: different
    assert is_isomorphic(a, b, structure_only)  # topology: same
    assert diff(a, b, structure_only).identical


def test_no_false_isomorphism_from_punctuated_labels():
    # A naive `label(child,child)` string canonical would render BOTH of these as
    # "x(a(),b())" and call them isomorphic. They are NOT: two children vs one.
    two_children = _fanout("x", "a", "b")
    one_punctuated_child = _fanout("x", "a(),b")
    assert not is_isomorphic(two_children, one_punctuated_child)
    assert not diff(two_children, one_punctuated_child).identical


def test_deep_linear_chain_does_not_overflow_recursion():
    # Agent traces are deep-linear (one super-step per node); a recursive AHU post-order
    # overflowed Python's recursion limit at ~500 nodes. Both the identical path
    # (canonical/is_isomorphic) and the divergence path (pair/localize + _render) must run
    # iteratively at depth far beyond that.
    names = [f"n{i}" for i in range(3000)]
    a, b = _chain(*names), _chain(*names)
    assert diff(a, b).identical
    result = diff(a, _chain(*names[:-1], "BOOM"))
    assert not result.identical
    assert any("BOOM" in c for c in result.changes)


def test_multiple_changed_siblings_are_not_cross_paired():
    # Two unrelated unmatched siblings on each side must be reported as add/remove, NOT
    # fabricated into a false "diverges at: A=<one> B=<other>" correspondence (the bug:
    # positional pop() paired p<->m and q<->n though they have nothing to do with each other).
    result = diff(_fanout("r", "p", "q"), _fanout("r", "m", "n"))
    assert not result.identical
    assert not any("diverges at" in c for c in result.changes), result.changes
    assert any("only in A" in c and "p" in c for c in result.changes)
    assert any("only in A" in c and "q" in c for c in result.changes)
    assert any("only in B" in c and "m" in c for c in result.changes)
    assert any("only in B" in c and "n" in c for c in result.changes)


def test_sibling_relabel_with_shared_children_is_localized():
    # The flip side of the above: two siblings that keep their child subtree but change their
    # OWN label ARE a real correspondence (a relabel) — they share non-empty child structure,
    # so they should be paired and reported as precise divergences, not blunt add/remove.
    def tree(tid: str, first: str, second: str):
        steps = [
            Step(step_id=f"{tid}r", trace_id=tid, seq=0, name="r"),
            Step(step_id=f"{tid}a", trace_id=tid, seq=1, name=first),
            Step(step_id=f"{tid}ac", trace_id=tid, seq=2, name="x"),
            Step(step_id=f"{tid}b", trace_id=tid, seq=3, name=second),
            Step(step_id=f"{tid}bc", trace_id=tid, seq=4, name="y"),
        ]
        edges = [
            Edge(type=EdgeType.CAUSED_BY, src=f"{tid}a", dst=f"{tid}r"),
            Edge(type=EdgeType.CAUSED_BY, src=f"{tid}ac", dst=f"{tid}a"),
            Edge(type=EdgeType.CAUSED_BY, src=f"{tid}b", dst=f"{tid}r"),
            Edge(type=EdgeType.CAUSED_BY, src=f"{tid}bc", dst=f"{tid}b"),
        ]
        return normalize(
            RawTrace(trace=Trace(trace_id=tid, source_kind="x"), steps=steps, causal_edges=edges)
        )

    result = diff(tree("A", "p", "q"), tree("B", "P", "Q"))  # p(x),q(y) vs P(x),Q(y)
    assert not result.identical
    assert any("A='p'" in c and "B='P'" in c for c in result.changes), result.changes
    assert any("A='q'" in c and "B='Q'" in c for c in result.changes), result.changes
