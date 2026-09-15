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
    EdgeOrigin,
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


def test_origin_priority_graph_parent_over_span_link_regardless_of_seq():
    # b happened earlier (seq 1), but is only a SPAN_LINK.
    # c happened later (seq 2), but is the explicit GRAPH_PARENT.
    # TREE_PARENT must pick c (origin prio 0 over 2), not b.
    raw = RawTrace(
        trace=Trace(trace_id="t_prio", source_kind="test"),
        steps=[
            Step(step_id="a", trace_id="t_prio", seq=0, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t_prio", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_prio", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_prio", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_LINK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.GRAPH_PARENT),
        ],
    )
    nt = normalize(raw)
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    assert d_parents == ["c"]


def test_origin_priority_checkpoint_parent_over_span_parent_fallback():
    # b is SPAN_PARENT_FALLBACK (containment, seq 1).
    # c is CHECKPOINT_PARENT (task parent, seq 2).
    # CHECKPOINT_PARENT (0) must outrank SPAN_PARENT_FALLBACK (1).
    raw = RawTrace(
        trace=Trace(trace_id="t_prio2", source_kind="test"),
        steps=[
            Step(step_id="b", trace_id="t_prio2", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_prio2", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_prio2", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_PARENT_FALLBACK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.CHECKPOINT_PARENT),
        ],
    )
    nt = normalize(raw)
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    assert d_parents == ["c"]


def test_origin_priority_span_parent_fallback_over_span_link():
    # b is SPAN_LINK (cross-reference, seq 1).
    # c is SPAN_PARENT_FALLBACK (containment parent, seq 2).
    # SPAN_PARENT_FALLBACK (1) must outrank SPAN_LINK (2).
    raw = RawTrace(
        trace=Trace(trace_id="t_prio3", source_kind="test"),
        steps=[
            Step(step_id="b", trace_id="t_prio3", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_prio3", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_prio3", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_LINK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.SPAN_PARENT_FALLBACK),
        ],
    )
    nt = normalize(raw)
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    assert d_parents == ["c"]


def test_origin_priority_span_link_over_legacy_unknown():
    # b is LEGACY_UNKNOWN (seq 1).
    # c is SPAN_LINK (seq 2).
    # SPAN_LINK (2) must outrank LEGACY_UNKNOWN (3).
    raw = RawTrace(
        trace=Trace(trace_id="t_prio4", source_kind="test"),
        steps=[
            Step(step_id="b", trace_id="t_prio4", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_prio4", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_prio4", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.LEGACY_UNKNOWN),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.SPAN_LINK),
        ],
    )
    nt = normalize(raw)
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    assert d_parents == ["c"]


def test_origin_priority_tie_breaking_by_earliest_seq():
    # Both are tier 0 (GRAPH_PARENT and CHECKPOINT_PARENT).
    # Earliest sequence (b @ seq 1 < c @ seq 2) must win the tie-breaker.
    raw = RawTrace(
        trace=Trace(trace_id="t_prio5", source_kind="test"),
        steps=[
            Step(step_id="b", trace_id="t_prio5", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_prio5", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_prio5", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.CHECKPOINT_PARENT),
        ],
    )
    nt = normalize(raw)
    d_parents = [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"]
    assert d_parents == ["b"]


def test_legacy_temporal_artifact_loads_validates_and_roundtrips():
    import hashlib
    import pytest
    from tracegraph.normalize import validate_normalized

    raw = RawTrace(
        trace=Trace(trace_id="t_legacy", source_kind="test"),
        steps=[
            Step(step_id="a", trace_id="t_legacy", seq=0, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t_legacy", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_legacy", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_legacy", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_LINK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.GRAPH_PARENT),
        ],
    )

    # 1. Produce artifact under the previous implementation's temporal-only projection
    legacy_nt = normalize(raw, origin_priority=False)
    assert [e.dst for e in legacy_nt.edges_of(EdgeType.TREE_PARENT) if e.src == "d"] == ["b"]

    # 2. Serialize and hash
    text = artifact.dumps(legacy_nt)
    original_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # 3. Load from JSON string
    loaded_nt = artifact.loads(text)
    assert loaded_nt == legacy_nt

    # 4. validate_normalized must accept authentic legacy-projected artifacts
    validate_normalized(loaded_nt)

    # 5. InMemoryStore round-trip
    in_mem_store = InMemoryStore.from_trace(loaded_nt)
    assert in_mem_store.trace() == loaded_nt

    # 6. LadybugStore byte-for-byte digest stability
    try:
        from tracegraph.store.ladybug import LadybugStore
        with LadybugStore.from_trace(loaded_nt) as ladybug_store:
            assert ladybug_store.trace() == loaded_nt
            reloaded_text = artifact.dumps(ladybug_store.trace())
            assert hashlib.sha256(reloaded_text.encode("utf-8")).hexdigest() == original_digest
    except ImportError:
        pass

    # 7. Non-canonical tampered artifact must still be rejected
    tampered_edges = [
        Edge(type=EdgeType.TREE_PARENT, src="d", dst="a") if e.type is EdgeType.TREE_PARENT and e.src == "d"
        else e
        for e in loaded_nt.edges
    ]
    tampered_nt = loaded_nt.model_copy(update={"edges": tampered_edges})
    with pytest.raises(ValueError, match="canonical form"):
        validate_normalized(tampered_nt)


def test_cross_version_schema_signaling_for_origin_priority_and_legacy(monkeypatch):
    import json
    import pytest

    raw = RawTrace(
        trace=Trace(trace_id="t_versioning", source_kind="test"),
        steps=[
            Step(step_id="a", trace_id="t_versioning", seq=0, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t_versioning", seq=1, kind=StepKind.TOOL),
            Step(step_id="c", trace_id="t_versioning", seq=2, kind=StepKind.TOOL),
            Step(step_id="d", trace_id="t_versioning", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.GRAPH_PARENT),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="b", origin=EdgeOrigin.SPAN_LINK),
            Edge(type=EdgeType.CAUSED_BY, src="d", dst="c", origin=EdgeOrigin.GRAPH_PARENT),
        ],
    )

    # A trace where origin priority changes TREE_PARENT must stamp schema_version 4
    origin_nt = normalize(raw, origin_priority=True)
    origin_text = artifact.dumps(origin_nt)
    assert json.loads(origin_text)["schema_version"] == 4

    # A legacy-projected trace (or one where origin priority doesn't change TREE_PARENT) stamps 2
    legacy_nt = normalize(raw, origin_priority=False)
    legacy_text = artifact.dumps(legacy_nt)
    assert json.loads(legacy_text)["schema_version"] == 2

    # A simulated older reader (supporting only versions 1, 2, 3) must reject the schema 4
    # artifact cleanly at the load boundary with a clear schema_version error
    monkeypatch.setattr(artifact, "_READABLE_VERSIONS", (1, 2, 3))
    with pytest.raises(ValueError, match="unsupported artifact schema_version 4"):
        artifact.loads(origin_text)

    # And the older reader must continue to load the schema 2 legacy artifact without error
    loaded_legacy = artifact.loads(legacy_text)
    assert loaded_legacy == legacy_nt
