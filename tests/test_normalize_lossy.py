"""The golden lossy-projection test — the guard on the correctness core.

Fan-in fixture::

        a            (root, seq 0)
       / \\
      b   c          (seq 1, 2)  -- both caused by a
       \\ /
        d            (seq 3)     -- caused by BOTH b and c  => projection is lossy

The single-parent tree must pick exactly one parent for ``d``; the raw graph must
keep both real causes; and ``explain`` must report that ``d``'s causality was lossy.
If any of these break, vocabulary/schema drift has crept in.
"""

from tracegraph import artifact
from tracegraph.analysis import explain
from tracegraph.model import (
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize, validate_tree
from tracegraph.store import InMemoryStore


def _fan_in() -> RawTrace:
    return RawTrace(
        trace=Trace(trace_id="t", source_kind="test", thread_id="A"),
        steps=[
            Step(step_id="a", trace_id="t", seq=0, source=StepSource.INPUT, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t", seq=3, kind=StepKind.CHAIN, status=StepStatus.ERROR,
                 error_msg="join failed"),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a"),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b"),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c"),
        ],
    )


def test_raw_layer_keeps_every_cause():
    nt = normalize(_fan_in())
    d_causes = {e.dst for e in nt.edges_of(EdgeType.CAUSED_BY) if e.src == "d"}
    assert d_causes == {"b", "c"}, "raw CAUSED_BY must preserve both real causes of d"


def test_derived_tree_picks_exactly_one_parent():
    nt = normalize(_fan_in())
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    # Exactly one, and it is the temporally-earliest cause (b @ seq1 < c @ seq2).
    assert d_parents == ["b"]


def test_projection_lossy_flag():
    nt = normalize(_fan_in())
    by_id = nt.steps_by_id()
    assert by_id["d"].projection_lossy is True
    assert all(not by_id[x].projection_lossy for x in ("a", "b", "c"))


def test_validate_tree_passes_on_forest():
    nt = normalize(_fan_in())
    validate_tree(nt)  # must not raise


def test_explain_reads_raw_layer_and_flags_lossiness():
    nt = normalize(_fan_in())
    store = InMemoryStore.from_trace(nt)
    result = explain(store, "d")

    chain_ids = {s.step_id for s in result.chain}
    assert chain_ids == {"a", "b", "c"}, "explain must see ALL real causes, not just the tree parent"
    assert result.is_lossy
    assert result.lossy_steps == ["d"]


def test_artifact_round_trip_preserves_both_layers():
    nt = normalize(_fan_in())
    again = artifact.loads(artifact.dumps(nt))
    assert again == nt
    validate_tree(again)


def test_validate_tree_rejects_multi_parent():
    import pytest

    nt = normalize(_fan_in())
    # Inject a second TREE_PARENT for d -> not a forest anymore.
    nt.edges.append(Edge(type=EdgeType.TREE_PARENT, src="d", dst="c"))
    with pytest.raises(ValueError, match="more than one TREE_PARENT"):
        validate_tree(nt)
