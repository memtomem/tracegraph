"""KuzuStore tests — gated on the ``[cypher]`` extra.

The headline contract: for any normalized trace and any ``PathPattern``, the Kùzu backend
must return the **same match set** as the pure-Python matcher. The Cypher backend is an
*accelerator*, not a separate analysis — divergence here would silently produce different
RCA evidence depending on which backend a user happened to install.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

kuzu = pytest.importorskip("kuzu", reason="requires the [cypher] extra")

from tracegraph import artifact
from tracegraph.analysis import PRESETS, PathPattern, StepPredicate, find_matches
from tracegraph.cli import app
from tracegraph.model import (
    Edge,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize
from tracegraph.store import KuzuStore

pytestmark = pytest.mark.cypher
runner = CliRunner()


# --- fixtures (shared trace shapes; mirror test_patterns.py so equivalence is checkable) ---


def _linear(tid: str, *specs) -> NormalizedTrace:
    """Linear cause→effect chain: specs[0] is the root; each next step is caused by the prior."""
    steps, edges = [], []
    for i, (name, kind, status) in enumerate(specs):
        steps.append(
            Step(
                step_id=f"{tid}{i}",
                trace_id=tid,
                seq=i,
                name=name,
                kind=kind,
                status=status,
            )
        )
        if i:
            edges.append(
                Edge(type=EdgeType.CAUSED_BY, src=f"{tid}{i}", dst=f"{tid}{i - 1}")
            )
    return normalize(
        RawTrace(trace=Trace(trace_id=tid, source_kind="x"), steps=steps, causal_edges=edges)
    )


OK = StepStatus.OK
ERR = StepStatus.ERROR


def _erroring_tool_trace(tid: str = "A") -> NormalizedTrace:
    return _linear(
        tid,
        ("input", StepKind.CHAIN, OK),
        ("plan", StepKind.CHAIN, OK),
        ("call_tool", StepKind.TOOL, ERR),
        ("respond", StepKind.CHAIN, OK),
    )


def _clean_trace(tid: str = "B") -> NormalizedTrace:
    return _linear(
        tid,
        ("input", StepKind.CHAIN, OK),
        ("plan", StepKind.CHAIN, OK),
        ("call_tool", StepKind.TOOL, OK),
        ("respond", StepKind.CHAIN, OK),
    )


def _fanin_trace(tid: str = "C") -> NormalizedTrace:
    """A step with two causes — the case where TREE_PARENT is lossy and the raw layer matters."""
    steps = [
        Step(step_id=f"{tid}0", trace_id=tid, seq=0, name="input", kind=StepKind.CHAIN),
        Step(step_id=f"{tid}1", trace_id=tid, seq=1, name="plan_a", kind=StepKind.CHAIN),
        Step(step_id=f"{tid}2", trace_id=tid, seq=2, name="plan_b", kind=StepKind.CHAIN),
        Step(
            step_id=f"{tid}3",
            trace_id=tid,
            seq=3,
            name="merge",
            kind=StepKind.TOOL,
            status=ERR,
        ),
    ]
    edges = [
        Edge(type=EdgeType.CAUSED_BY, src=f"{tid}1", dst=f"{tid}0"),
        Edge(type=EdgeType.CAUSED_BY, src=f"{tid}2", dst=f"{tid}0"),
        Edge(type=EdgeType.CAUSED_BY, src=f"{tid}3", dst=f"{tid}1"),
        Edge(type=EdgeType.CAUSED_BY, src=f"{tid}3", dst=f"{tid}2"),
    ]
    return normalize(
        RawTrace(trace=Trace(trace_id=tid, source_kind="x"), steps=steps, causal_edges=edges)
    )


# --- equivalence: same input → same matches (the central contract) ---


@pytest.mark.parametrize("preset_name", sorted(PRESETS.keys()))
def test_pattern_matches_equivalent_to_pure_python(preset_name: str) -> None:
    # Every shipped preset, on every shape we test elsewhere, must agree across backends —
    # exact list equality (not set equality): if Kùzu reorders matches relative to the
    # pure-Python traversal, that's a real divergence we want to catch, not paper over.
    pattern = PRESETS[preset_name]
    for nt in (_erroring_tool_trace(), _clean_trace(), _fanin_trace()):
        store = KuzuStore.from_trace(nt)
        assert store.find_matches(pattern) == find_matches(nt, pattern), (
            f"divergence on {preset_name} / {nt.trace.trace_id}"
        )


def test_ad_hoc_pattern_with_only_kind_wildcard_status_matches_python() -> None:
    # Exercise a non-preset pattern so the equivalence isn't preset-shaped by accident.
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(kind=StepKind.CHAIN), StepPredicate(kind=StepKind.TOOL)))
    store = KuzuStore.from_trace(nt)
    assert store.find_matches(pattern) == find_matches(nt, pattern)


def test_fanin_pattern_returns_one_row_per_real_cause_in_traversal_order() -> None:
    # The order-sensitive equivalence above could trivially pass on linear traces where
    # there's only ever one match per outer step. This case has TWO matches that share a
    # column position (input -> plan_a -> merge AND input -> plan_b -> merge), so we
    # exercise the per-column seq tiebreaker explicitly.
    nt = _fanin_trace()
    pattern = PathPattern((StepPredicate(kind=StepKind.CHAIN), StepPredicate(kind=StepKind.TOOL)))
    store = KuzuStore.from_trace(nt)
    ku = store.find_matches(pattern)
    py = find_matches(nt, pattern)
    assert ku == py
    assert len(ku) >= 2  # guard: if this drops to one, the trace shape is no longer fan-in


def test_empty_pattern_returns_no_matches_on_both_backends() -> None:
    # find_matches in pure-Python short-circuits; KuzuStore must match the convention so
    # backend-agnostic callers don't need a special case.
    nt = _erroring_tool_trace()
    assert KuzuStore.from_trace(nt).find_matches(PathPattern(())) == []
    assert find_matches(nt, PathPattern(())) == []


# --- GraphStore protocol parity ---


def test_ancestors_returns_full_raw_chain_nearest_first_on_linear_trace() -> None:
    # On a linear trace BFS == seq-descending, so the order is deterministic and equal to
    # InMemoryStore.ancestors. (The fan-in case below only asserts the set, since BFS order
    # over multi-parent steps isn't an externally-stable contract.)
    nt = _erroring_tool_trace()
    store = KuzuStore.from_trace(nt)
    chain = store.ancestors("A3")  # respond <- call_tool <- plan <- input
    assert [s.step_id for s in chain] == ["A2", "A1", "A0"]


def test_ancestors_on_fanin_returns_every_real_cause() -> None:
    # The whole point of the raw layer: a fan-in step must surface BOTH causes, not just
    # the TREE_PARENT projection. (Order is unspecified for multi-parent, so compare sets.)
    nt = _fanin_trace()
    store = KuzuStore.from_trace(nt)
    chain = store.ancestors("C3")
    assert {s.step_id for s in chain} == {"C0", "C1", "C2"}


def test_ancestors_raises_on_unknown_step_id() -> None:
    store = KuzuStore.from_trace(_erroring_tool_trace())
    with pytest.raises(KeyError, match="unknown step"):
        store.ancestors("does-not-exist")


def test_artifact_roundtrip_preserves_normalized_trace(tmp_path) -> None:
    # The artifact is the system of record. Loading into Kùzu and exporting must reproduce
    # the JSON byte-for-byte — otherwise the Cypher backend isn't a true cache, it's a
    # second source that can drift.
    nt = _fanin_trace()
    src_path = tmp_path / "trace.json"
    artifact.save(nt, src_path)

    store = KuzuStore.load_artifact(src_path)
    out_path = tmp_path / "out.json"
    store.export_artifact(out_path)

    assert out_path.read_bytes() == src_path.read_bytes()


def test_roundtrip_is_byte_stable_even_for_non_canonical_raw_input(tmp_path) -> None:
    # The byte-stability claim is "any RawTrace that normalize() accepts round-trips" —
    # not "any RawTrace that happens to be in canonical order". Construct a RawTrace with
    # steps in reverse-seq order and CAUSED_BY edges in reverse-encountered order; after
    # one normalize() pass it should land on the canonical artifact, and KuzuStore round-
    # trip must hold from there.
    steps = [
        Step(step_id="r3", trace_id="R", seq=3, name="merge", kind=StepKind.TOOL, status=ERR),
        Step(step_id="r2", trace_id="R", seq=2, name="b", kind=StepKind.CHAIN),
        Step(step_id="r1", trace_id="R", seq=1, name="a", kind=StepKind.CHAIN),
        Step(step_id="r0", trace_id="R", seq=0, name="input", kind=StepKind.CHAIN),
    ]
    edges = [
        Edge(type=EdgeType.CAUSED_BY, src="r3", dst="r2"),
        Edge(type=EdgeType.CAUSED_BY, src="r3", dst="r1"),
        Edge(type=EdgeType.CAUSED_BY, src="r2", dst="r0"),
        Edge(type=EdgeType.CAUSED_BY, src="r1", dst="r0"),
    ]
    nt = normalize(
        RawTrace(trace=Trace(trace_id="R", source_kind="x"), steps=steps, causal_edges=edges)
    )
    src_path = tmp_path / "trace.json"
    artifact.save(nt, src_path)

    out_path = tmp_path / "out.json"
    KuzuStore.load_artifact(src_path).export_artifact(out_path)

    assert out_path.read_bytes() == src_path.read_bytes()


def test_from_raw_normalizes_and_loads() -> None:
    # The from_raw / from_trace convenience constructors are the public entry points the
    # CLI uses — they must both produce a queryable store.
    nt = _erroring_tool_trace()
    raw = RawTrace(
        trace=nt.trace,
        steps=[
            Step(**s.model_dump(exclude={"projection_lossy"})) for s in nt.steps
        ],
        causal_edges=nt.edges_of(EdgeType.CAUSED_BY),
    )
    store = KuzuStore.from_raw(raw)
    assert len(store.trace().steps) == len(nt.steps)


def test_corrupt_trace_rejected_at_load_boundary() -> None:
    # Same boundary check as InMemoryStore: dangling edges must be caught before they
    # enter the DB, where they'd silently disappear (Kùzu's MATCH-then-CREATE skips
    # missing endpoints, which would hide the bug).
    nt = _erroring_tool_trace()
    bad_edges = nt.edges + [Edge(type=EdgeType.CAUSED_BY, src="A3", dst="ghost")]
    corrupt = NormalizedTrace(trace=nt.trace, steps=nt.steps, edges=bad_edges)
    with pytest.raises(ValueError, match="ghost"):
        KuzuStore.from_trace(corrupt)


# --- CLI backend selection ---


def test_cli_explain_can_use_kuzu_backend(tmp_path) -> None:
    src_path = tmp_path / "trace.json"
    artifact.save(_erroring_tool_trace(), src_path)

    res = runner.invoke(app, ["explain", "--backend", "kuzu", str(src_path), "A2"])

    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output
    assert "← plan" in res.output


def test_cli_query_can_use_kuzu_backend(tmp_path) -> None:
    a_path = tmp_path / "A.json"
    b_path = tmp_path / "B.json"
    artifact.save(_erroring_tool_trace(), a_path)
    artifact.save(_clean_trace(), b_path)

    res = runner.invoke(
        app,
        ["query", "tool-failure", "--backend", "kuzu", str(a_path), str(b_path)],
    )

    assert res.exit_code == 0, res.output
    assert "A" in res.output
    assert "call_tool" in res.output
    assert "1 match(es)" in res.output
