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
