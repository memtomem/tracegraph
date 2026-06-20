"""Negative-path guards: malformed raw input must be rejected, not silently mangled."""

import pytest

from tracegraph import artifact
from tracegraph.model import Edge, EdgeType, NormalizedTrace, RawTrace, Step, StepStatus, Trace
from tracegraph.normalize import normalize, validate_normalized, validate_tree
from tracegraph.store import InMemoryStore


def _steps(*id_seq: tuple[str, int]) -> list[Step]:
    return [Step(step_id=i, trace_id="t", seq=s) for i, s in id_seq]


def _raw(steps: list[Step], edges: list[Edge]) -> RawTrace:
    return RawTrace(trace=Trace(trace_id="t", source_kind="test"), steps=steps, causal_edges=edges)


def test_unknown_cause_rejected():
    raw = _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="ghost")])
    with pytest.raises(ValueError, match="not a known step"):
        normalize(raw)


def test_normalize_output_is_canonically_ordered_independent_of_input_order():
    """The contract Kùzu (and any other cache) relies on: ``normalize`` produces the same
    artifact bytes regardless of how the adapter happened to enumerate steps and edges.

    We feed the SAME logical trace twice — once in canonical (seq-ASC) order, once
    deliberately scrambled — and assert the two normalized outputs are identical."""
    s = _steps(("a", 0), ("b", 1), ("c", 2))
    canonical_edges = [
        Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
        Edge(type=EdgeType.CAUSED_BY, src="c", dst="b"),
    ]
    scrambled_edges = list(reversed(canonical_edges))
    scrambled_steps = list(reversed(s))

    nt_canonical = normalize(_raw(s, canonical_edges))
    nt_scrambled = normalize(_raw(scrambled_steps, scrambled_edges))

    assert nt_canonical == nt_scrambled
    # And the canonical layout itself: steps in seq order, CAUSED_BY before derived edges.
    assert [step.step_id for step in nt_canonical.steps] == ["a", "b", "c"]
    assert [(e.type, e.src, e.dst) for e in nt_canonical.edges_of(EdgeType.CAUSED_BY)] == [
        (EdgeType.CAUSED_BY, "b", "a"),
        (EdgeType.CAUSED_BY, "c", "b"),
    ]


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


def test_duplicate_caused_by_edge_rejected():
    raw = _raw(
        _steps(("a", 0), ("b", 1)),
        [
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a"),
        ],
    )
    with pytest.raises(ValueError, match="duplicate CAUSED_BY"):
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


# --- "is exactly what normalize() would produce" boundary tests -----------------------
#
# These guard the stronger validate_normalized contract: a NormalizedTrace at the load
# boundary must equal normalize(raw_layer). Anything less and a backend rebuilding from
# the raw layer (KuzuStore) would silently emit different bytes — drift the artifact
# format is meant to prevent.


def _canonical_two_step() -> NormalizedTrace:
    return normalize(
        _raw(_steps(("a", 0), ("b", 1)), [Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")])
    )


def test_validate_normalized_rejects_missing_belongs_to():
    nt = _canonical_two_step()
    stripped = nt.model_copy(
        update={"edges": [e for e in nt.edges if e.type is not EdgeType.BELONGS_TO]}
    )
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(stripped)


def test_validate_normalized_rejects_missing_tree_parent():
    nt = _canonical_two_step()
    stripped = nt.model_copy(
        update={"edges": [e for e in nt.edges if e.type is not EdgeType.TREE_PARENT]}
    )
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(stripped)


def test_validate_normalized_rejects_wrong_projection_lossy_flag():
    # The flag is a derived signal; a hand-edited artifact that claims a single-cause step
    # was lossy (or vice versa) would mislead RCA — catch it at the boundary.
    nt = _canonical_two_step()
    tampered_steps = [
        s.model_copy(update={"projection_lossy": True}) if s.step_id == "b" else s
        for s in nt.steps
    ]
    tampered = nt.model_copy(update={"steps": tampered_steps})
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(tampered)


def test_validate_normalized_rejects_non_canonical_edge_order():
    # Same logical trace, edges shuffled out of canonical order — must be rejected so the
    # KuzuStore (which sorts queries canonically) can't silently disagree with a backend
    # that walked the edges in input order.
    nt = _canonical_two_step()
    shuffled = nt.model_copy(update={"edges": list(reversed(nt.edges))})
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(shuffled)


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


# --- trace-level status is part of the system of record ------------------------------------


def test_validate_normalized_rejects_lying_trace_status():
    # A clean-looking status on a trace whose steps tell a different story would let an
    # artifact hide a failed run. The header is system-of-record truth, so reject it.
    nt = _canonical_two_step()  # no error steps -> status ok
    lying = nt.model_copy(
        update={"trace": nt.trace.model_copy(update={"status": StepStatus.ERROR})}
    )
    with pytest.raises(ValueError, match="disagrees with its steps"):
        validate_normalized(lying)


def test_normalize_derives_trace_status_from_steps():
    # normalize() makes trace.status a derived view of the steps regardless of what the raw
    # trace claimed: an erroring step always yields status=error, and the result self-checks.
    steps = [
        Step(step_id="a", trace_id="t", seq=0),
        Step(step_id="b", trace_id="t", seq=1, status=StepStatus.ERROR, error_msg="boom"),
    ]
    raw = RawTrace(
        trace=Trace(trace_id="t", source_kind="test", status=StepStatus.OK),  # raw lies: ok
        steps=steps,
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="b", dst="a")],
    )
    nt = normalize(raw)
    assert nt.trace.status is StepStatus.ERROR
    validate_normalized(nt)  # the derived status passes its own consistency check
