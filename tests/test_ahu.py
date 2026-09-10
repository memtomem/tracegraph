"""Rooted-tree (AHU) isomorphism + structural diff."""

import pytest

from tracegraph.analysis import canonical, diff, is_isomorphic, structure_only
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
    # (canonical/is_isomorphic) and the divergence path (pair/localize + render) must run
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


def test_single_unrelated_leftover_per_side_is_not_cross_paired():
    # The subtler version: after aligning the shared child `c`, each side has exactly ONE
    # leftover leaf (p vs m) with nothing in common. "one leftover each" is not evidence of
    # correspondence, so this must be add/remove — never a fabricated "diverges at A=p B=m".
    result = diff(_fanout("r", "c", "p"), _fanout("r", "c", "m"))
    assert not result.identical
    assert not any("diverges at" in c for c in result.changes), result.changes
    assert any("only in A" in c and "p" in c for c in result.changes)
    assert any("only in B" in c and "m" in c for c in result.changes)


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


def _cyclic_tree_layer():
    """A trace whose derived TREE_PARENT layer contains a 2-cycle (b <-> c).

    Built by hand: normalize() would never emit this, but a hand-edited or partly-corrupt
    artifact can carry it, and loads() does not validate. Steps b and c then have a
    TREE_PARENT edge each, so neither is a root and the whole component is unreachable.
    """
    steps = [
        Step(step_id="a", trace_id="t", seq=0, name="a"),
        Step(step_id="b", trace_id="t", seq=1, name="b"),
        Step(step_id="c", trace_id="t", seq=2, name="c"),
    ]
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id="t", source_kind="x"),
            steps=steps,
            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
                          Edge(type=EdgeType.CAUSED_BY, src="c", dst="a")],
        )
    )
    nt.edges = [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT] + [
        Edge(type=EdgeType.TREE_PARENT, src="b", dst="c"),
        Edge(type=EdgeType.TREE_PARENT, src="c", dst="b"),
    ]
    return nt


def _tree_layer(nt, *pairs):
    """Replace the derived TREE_PARENT layer with `pairs` of (child, parent)."""
    nt.edges = [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT] + [
        Edge(type=EdgeType.TREE_PARENT, src=child, dst=parent) for child, parent in pairs
    ]
    return nt


def _three_step_trace():
    steps = [
        Step(step_id="a", trace_id="t", seq=0, name="a"),
        Step(step_id="b", trace_id="t", seq=1, name="b"),
        Step(step_id="c", trace_id="t", seq=2, name="c"),
    ]
    return normalize(
        RawTrace(
            trace=Trace(trace_id="t", source_kind="x"),
            steps=steps,
            causal_edges=[
                Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
                Edge(type=EdgeType.CAUSED_BY, src="c", dst="a"),
            ],
        )
    )


def _every_entry_point(bad):
    just_a = _chain("a")
    return (
        lambda: diff(bad, just_a),
        lambda: diff(just_a, bad),
        lambda: is_isomorphic(bad, just_a),
        lambda: canonical(bad),
    )


def test_disconnected_cycle_is_an_error_not_a_silent_subset():
    """Dropping unreachable nodes let two different traces compare as identical."""
    bad = _cyclic_tree_layer()
    for call in _every_entry_point(bad):
        with pytest.raises(ValueError, match="not a forest"):
            call()


def test_cycle_reachable_from_a_root_is_rejected_rather_than_walked_forever():
    """A guard placed after traversal never runs: this shape loops until interrupted.

    ``b`` has both a real parent (``a``, reachable from the root) and a self-edge, so ``a``
    is still a root and the walk from it re-expands ``b`` endlessly.
    """
    bad = _tree_layer(_three_step_trace(), ("b", "a"), ("b", "b"))
    for call in _every_entry_point(bad):
        with pytest.raises(ValueError, match="not a forest"):
            call()


def test_tree_parent_edge_to_an_unknown_step_is_rejected():
    bad = _tree_layer(_three_step_trace(), ("b", "ghost"))
    for call in _every_entry_point(bad):
        with pytest.raises(ValueError, match="not a forest"):
            call()


def test_duplicate_step_ids_are_rejected_with_a_clear_message():
    """A count-based coverage check reported this as an empty "missing" list and crashed."""
    bad = _three_step_trace()
    bad.steps.append(bad.steps[0].model_copy())
    for call in _every_entry_point(bad):
        with pytest.raises(ValueError, match="duplicate step_id"):
            call()
