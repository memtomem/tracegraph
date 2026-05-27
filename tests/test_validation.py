"""Negative-path guards: malformed raw input must be rejected, not silently mangled."""

import pytest

from tracegraph import artifact
from tracegraph.model import Edge, EdgeType, NormalizedTrace, RawTrace, Step, Trace
from tracegraph.normalize import normalize, validate_tree
from tracegraph.store import InMemoryStore


def _steps(*id_seq: tuple[str, int]) -> list[Step]:
    return [Step(step_id=i, trace_id="t", seq=s) for i, s in id_seq]


def _raw(steps: list[Step], edges: list[Edge]) -> RawTrace:
    return RawTrace(trace=Trace(trace_id="t", source_kind="test"), steps=steps, causal_edges=edges)


def test_unknown_cause_rejected():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")])
    with pytest.raises(ValueError, match="not a known step"):
        normalize(raw)


def test_unknown_effect_rejected():
    raw = _raw(_steps(("a", 0)), [Edge(type=EdgeType.CAUSED_BY, src="ghost", dst="a")])
    with pytest.raises(ValueError, match="not a known step"):
        normalize(raw)


def test_self_edge_rejected():
    raw = _raw(_steps(("a", 0)), [Edge(type=EdgeType.CAUSED_BY, src="a", dst="a")])
    with pytest.raises(ValueError, match="self-causal"):
        normalize(raw)


def test_cause_must_precede_effect():
    # b(seq1) "caused by" c(seq2): the cause is later than the effect -> reject.
    # This rule also makes raw cycles impossible (seq can't strictly decrease in a loop).
    raw = _raw(_steps(("b", 1), ("c", 2)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="c")])
    with pytest.raises(ValueError, match="does not precede"):
        normalize(raw)


def test_non_caused_by_edge_in_raw_rejected():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.TREE_PARENT, src="b", dst="a")])
    with pytest.raises(ValueError, match="CAUSED_BY"):
        normalize(raw)


def test_duplicate_step_id_rejected():
    raw = _raw(
        [Step(step_id="a", trace_id="t", seq=0), Step(step_id="a", trace_id="t", seq=1)], []
    )
    with pytest.raises(ValueError, match="duplicate"):
        normalize(raw)


def test_trace_id_mismatch_rejected():
    raw = RawTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=[Step(step_id="a", trace_id="OTHER", seq=0)],
        causal_edges=[],
    )
    with pytest.raises(ValueError, match="trace_id"):
        normalize(raw)


def test_normalize_is_deterministic():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")])
    assert normalize(raw) == normalize(raw)


def test_ancestors_raises_on_dangling_edge():
    # Bypass the validating constructors to simulate a corrupted store.
    store = InMemoryStore()
    store.init_schema()
    store.upsert_nodes(_steps(("b", 1)))
    store.upsert_edges([Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")])
    with pytest.raises(KeyError, match="unknown step"):
        store.ancestors("b")


def test_validate_tree_detects_cycle():
    # Each step has exactly one TREE_PARENT (passes the forest single-parent check),
    # but a<->b forms a cycle -> must be caught.
    nt = NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1)),
        edges=[
            Edge(type=EdgeType.TREE_PARENT, src="a", dst="b"),
            Edge(type=EdgeType.TREE_PARENT, src="b", dst="a"),
        ],
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_tree(nt)


def test_from_trace_rejects_non_forest():
    # A NormalizedTrace that smuggled in two TREE_PARENTs must not load into a store.
    nt = NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1), ("c", 2)),
        edges=[
            Edge(type=EdgeType.TREE_PARENT, src="c", dst="a"),
            Edge(type=EdgeType.TREE_PARENT, src="c", dst="b"),
        ],
    )
    with pytest.raises(ValueError, match="more than one TREE_PARENT"):
        InMemoryStore.from_trace(nt)


def _corrupt_normalized() -> NormalizedTrace:
    # A NormalizedTrace whose RAW layer is broken: CAUSED_BY points to a missing step.
    return NormalizedTrace(
        trace=Trace(trace_id="t", source_kind="x"),
        steps=_steps(("a", 0), ("b", 1)),
        edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")],
    )


def test_from_trace_validates_raw_layer():
    with pytest.raises(ValueError, match="not a known step"):
        InMemoryStore.from_trace(_corrupt_normalized())


def test_load_artifact_rejects_corrupt_raw_layer(tmp_path):
    # artifact.save stays a pure serializer (no validation); the store load is the gate.
    path = tmp_path / "bad.json"
    artifact.save(_corrupt_normalized(), path)
    with pytest.raises(ValueError, match="not a known step"):
        InMemoryStore.load_artifact(path)
