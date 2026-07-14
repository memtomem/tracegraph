"""LadybugStore tests — gated on the ``[cypher]`` extra.

The headline contract: for any normalized trace and any ``PathPattern``, the LadybugDB backend
must return the **same match set** as the pure-Python matcher. The Cypher backend is an
*accelerator*, not a separate analysis — divergence here would silently produce different
RCA evidence depending on which backend a user happened to install.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

ladybug = pytest.importorskip("ladybug", reason="requires the [cypher] extra")

from tracegraph import artifact
from tracegraph.analysis import MAX_GAP, PRESETS, PathPattern, StepPredicate, find_matches
from tracegraph.cli import app
from tracegraph.model import (
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepEvidence,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize
from tracegraph.store import InMemoryStore, LadybugStore

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


def _custom(tid, specs, edges) -> NormalizedTrace:
    """Build a trace from explicit (local_id, name, kind, status) specs + CAUSED_BY id pairs."""
    steps = [
        Step(step_id=f"{tid}{sid}", trace_id=tid, seq=i, name=name, kind=kind, status=status)
        for i, (sid, name, kind, status) in enumerate(specs)
    ]
    es = [Edge(type=EdgeType.CAUSED_BY, src=f"{tid}{s}", dst=f"{tid}{d}") for s, d in edges]
    return normalize(
        RawTrace(trace=Trace(trace_id=tid, source_kind="x"), steps=steps, causal_edges=es)
    )


def _retry_trace(tid: str = "RT") -> NormalizedTrace:
    """'tool X → retry → tool X → failure': two 'search' tools 3 causal hops apart, 2nd errors."""
    return _custom(
        tid,
        [
            (0, "input", StepKind.CHAIN, OK),
            (1, "plan", StepKind.CHAIN, OK),
            (2, "search", StepKind.TOOL, OK),
            (3, "handle_error", StepKind.CHAIN, OK),
            (4, "replan", StepKind.CHAIN, OK),
            (5, "search", StepKind.TOOL, ERR),
        ],
        [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4)],
    )


def _retry_diamond(tid: str = "RD") -> NormalizedTrace:
    """search(OK) → {a, b} → search(ERR): the failing retry is reachable by TWO gap paths.

    This is the equivalence trap: a LadybugDB variable-length match yields one row per *path*, so
    without RETURN DISTINCT it would emit the (s0,s1) pair twice and diverge from the deduped
    pure-Python matcher.
    """
    return _custom(
        tid,
        [
            (0, "search", StepKind.TOOL, OK),
            (1, "a", StepKind.CHAIN, OK),
            (2, "b", StepKind.CHAIN, OK),
            (3, "search", StepKind.TOOL, ERR),
        ],
        [(1, 0), (2, 0), (3, 1), (3, 2)],
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
    # exact list equality (not set equality): if LadybugDB reorders matches relative to the
    # pure-Python traversal, that's a real divergence we want to catch, not paper over.
    # The retry fixtures exercise the gap presets across BOTH backend modes: the bounded
    # 'near' preset compiles to capped Cypher; the unbounded marquee falls back to pure-Python.
    pattern = PRESETS[preset_name]
    for nt in (
        _erroring_tool_trace(),
        _clean_trace(),
        _fanin_trace(),
        _retry_trace(),
        _retry_diamond(),
    ):
        store = LadybugStore.from_trace(nt)
        assert store.find_matches(pattern) == find_matches(nt, pattern), (
            f"divergence on {preset_name} / {nt.trace.trace_id}"
        )


def test_gap_diamond_distinct_parity_one_row_per_endpoint_pair() -> None:
    # The DISTINCT trap, head-on: the bounded 'near' preset compiles to a variable-length
    # match that, WITHOUT RETURN DISTINCT, returns the (search, search) pair once PER path —
    # twice on this diamond — diverging from the deduped pure-Python matcher. With DISTINCT
    # both yield exactly one row. (If this regresses, the compiled query dropped DISTINCT.)
    nt = _retry_diamond()
    store = LadybugStore.from_trace(nt)
    ku = store.find_matches(PRESETS["tool-retry-failure-near"])
    py = find_matches(nt, PRESETS["tool-retry-failure-near"])
    assert ku == py == [["RD0", "RD3"]]
    assert store.fell_back_to_python is False  # bounded gap → ran as real Cypher


def test_unbounded_marquee_falls_back_to_python_and_stays_equivalent() -> None:
    # The unbounded marquee can't compile under the 30-hop cap, so LadybugStore must transparently
    # run the pure-Python matcher (system of record) and flag that it did — never silently truncate.
    for nt in (_retry_trace(), _retry_diamond()):
        store = LadybugStore.from_trace(nt)
        ku = store.find_matches(PRESETS["tool-retry-failure"])
        assert store.fell_back_to_python is True
        assert ku == find_matches(nt, PRESETS["tool-retry-failure"])


def test_fell_back_flag_resets_between_calls() -> None:
    # The flag reflects the MOST RECENT call: a fallback then a compilable call must clear it,
    # or the CLI honesty note would cry wolf on a subsequent native-Cypher query.
    store = LadybugStore.from_trace(_retry_trace())
    store.find_matches(PRESETS["tool-retry-failure"])  # unbounded → fallback
    assert store.fell_back_to_python is True
    store.find_matches(PRESETS["tool-retry-failure-near"])  # bounded → native Cypher
    assert store.fell_back_to_python is False


def test_unbounded_fallback_matches_far_retry_past_the_hop_cap() -> None:
    # A retry well beyond LadybugDB's 30-hop cap: the bounded 'near' Cypher would miss it, but the
    # unbounded fallback (whole-graph pull + pure-Python) catches it AND equals the in-memory
    # store at that depth — the same load-then-traverse guarantee ancestors() makes.
    n = 80  # > MAX_GAP, so a *2..30 var-length query could never reach the failing retry
    specs = [(i, f"n{i}", StepKind.CHAIN, OK) for i in range(n)]
    specs[3] = (3, "search", StepKind.TOOL, OK)
    specs[n - 3] = (n - 3, "search", StepKind.TOOL, ERR)
    nt = _custom("DEEP", specs, [(i, i - 1) for i in range(1, n)])

    store = LadybugStore.from_trace(nt)
    ku = store.find_matches(PRESETS["tool-retry-failure"])
    assert store.fell_back_to_python is True
    assert ku == find_matches(nt, PRESETS["tool-retry-failure"]) == [["DEEP3", f"DEEP{n - 3}"]]
    assert (n - 3) - 3 > MAX_GAP  # guard: the retry really is past the cap
    # the bounded variant, run as native Cypher, correctly finds nothing this far apart
    assert store.find_matches(PRESETS["tool-retry-failure-near"]) == []


def test_ad_hoc_pattern_with_only_kind_wildcard_status_matches_python() -> None:
    # Exercise a non-preset pattern so the equivalence isn't preset-shaped by accident.
    nt = _erroring_tool_trace()
    pattern = PathPattern((StepPredicate(kind=StepKind.CHAIN), StepPredicate(kind=StepKind.TOOL)))
    store = LadybugStore.from_trace(nt)
    assert store.find_matches(pattern) == find_matches(nt, pattern)


def test_fanin_pattern_returns_one_row_per_real_cause_in_traversal_order() -> None:
    # The order-sensitive equivalence above could trivially pass on linear traces where
    # there's only ever one match per outer step. This case has TWO matches that share a
    # column position (input -> plan_a -> merge AND input -> plan_b -> merge), so we
    # exercise the per-column seq tiebreaker explicitly.
    nt = _fanin_trace()
    pattern = PathPattern((StepPredicate(kind=StepKind.CHAIN), StepPredicate(kind=StepKind.TOOL)))
    store = LadybugStore.from_trace(nt)
    ku = store.find_matches(pattern)
    py = find_matches(nt, pattern)
    assert ku == py
    assert len(ku) >= 2  # guard: if this drops to one, the trace shape is no longer fan-in


def test_empty_pattern_returns_no_matches_on_both_backends() -> None:
    # find_matches in pure-Python short-circuits; LadybugStore must match the convention so
    # backend-agnostic callers don't need a special case.
    nt = _erroring_tool_trace()
    assert LadybugStore.from_trace(nt).find_matches(PathPattern(())) == []
    assert find_matches(nt, PathPattern(())) == []


# --- GraphStore protocol parity ---


def test_ancestors_returns_full_raw_chain_nearest_first_on_linear_trace() -> None:
    # On a linear trace BFS == seq-descending, so the order is deterministic and equal to
    # InMemoryStore.ancestors. (The fan-in case below only asserts the set, since BFS order
    # over multi-parent steps isn't an externally-stable contract.)
    nt = _erroring_tool_trace()
    store = LadybugStore.from_trace(nt)
    chain = store.ancestors("A3")  # respond <- call_tool <- plan <- input
    assert [s.step_id for s in chain] == ["A2", "A1", "A0"]


def test_ancestors_on_fanin_returns_every_real_cause() -> None:
    # The whole point of the raw layer: a fan-in step must surface BOTH causes, not just
    # the TREE_PARENT projection. (Order is unspecified for multi-parent, so compare sets.)
    nt = _fanin_trace()
    store = LadybugStore.from_trace(nt)
    chain = store.ancestors("C3")
    assert {s.step_id for s in chain} == {"C0", "C1", "C2"}


def test_ancestors_raises_on_unknown_step_id() -> None:
    store = LadybugStore.from_trace(_erroring_tool_trace())
    with pytest.raises(KeyError, match="unknown step"):
        store.ancestors("does-not-exist")


def test_ancestors_deep_chain_matches_in_memory_at_any_depth() -> None:
    # Regression: LadybugDB caps variable-length hops at 30, so a `CAUSED_BY*0..` ancestor query
    # silently truncated chains deeper than 31 — a partial RCA with no error. ancestors()
    # must reproduce InMemoryStore exactly at any depth, including well past the 30-hop cap.
    n = 100
    nt = _linear("D", *[(f"n{i}", StepKind.CHAIN, OK) for i in range(n)])
    ku = LadybugStore.from_trace(nt).ancestors(f"D{n - 1}")
    mem = InMemoryStore.from_trace(nt).ancestors(f"D{n - 1}")
    assert [s.step_id for s in ku] == [s.step_id for s in mem]
    assert len(ku) == n - 1  # the full chain, not truncated at 31


def test_artifact_roundtrip_preserves_normalized_trace(tmp_path) -> None:
    # The artifact is the system of record. Loading into LadybugDB and exporting must reproduce
    # the JSON byte-for-byte — otherwise the Cypher backend isn't a true cache, it's a
    # second source that can drift.
    nt = _fanin_trace()
    src_path = tmp_path / "trace.json"
    artifact.save(nt, src_path)

    store = LadybugStore.load_artifact(src_path)
    out_path = tmp_path / "out.json"
    store.export_artifact(out_path)

    assert out_path.read_bytes() == src_path.read_bytes()


def test_v2_evidence_origin_and_run_id_survive_ladybug_roundtrip(tmp_path) -> None:
    raw = RawTrace(
        trace=Trace(
            trace_id="evidence",
            source_kind="phoenix_cli",
            run_id="run-1",
            causal_fidelity=CausalFidelity.PARENT_ONLY,
            links_preserved=False,
        ),
        steps=[
            Step(step_id="a", trace_id="evidence", seq=0, name="agent"),
            Step(
                step_id="b",
                trace_id="evidence",
                seq=1,
                name="llm",
                kind=StepKind.LLM,
                evidence=StepEvidence(total_tokens=12, total_cost="0.01"),
            ),
        ],
        causal_edges=[
            Edge(
                type=EdgeType.CAUSED_BY,
                src="b",
                dst="a",
                origin=EdgeOrigin.SPAN_PARENT_FALLBACK,
            )
        ],
    )
    nt = normalize(raw)
    assert LadybugStore.from_trace(nt).trace() == nt


def test_roundtrip_is_byte_stable_even_for_non_canonical_raw_input(tmp_path) -> None:
    # The byte-stability claim is "any RawTrace that normalize() accepts round-trips" —
    # not "any RawTrace that happens to be in canonical order". Construct a RawTrace with
    # steps in reverse-seq order and CAUSED_BY edges in reverse-encountered order; after
    # one normalize() pass it should land on the canonical artifact, and LadybugStore round-
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
    LadybugStore.load_artifact(src_path).export_artifact(out_path)

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
    store = LadybugStore.from_raw(raw)
    assert len(store.trace().steps) == len(nt.steps)


def test_corrupt_trace_rejected_at_load_boundary() -> None:
    # Same boundary check as InMemoryStore: dangling edges must be caught before they
    # enter the DB, where they'd silently disappear (LadybugDB's MATCH-then-CREATE skips
    # missing endpoints, which would hide the bug).
    nt = _erroring_tool_trace()
    bad_edges = nt.edges + [Edge(type=EdgeType.CAUSED_BY, src="A3", dst="ghost")]
    corrupt = NormalizedTrace(trace=nt.trace, steps=nt.steps, edges=bad_edges)
    with pytest.raises(ValueError, match="ghost"):
        LadybugStore.from_trace(corrupt)


# --- CLI backend selection ---


def test_cli_explain_can_use_ladybug_backend(tmp_path) -> None:
    src_path = tmp_path / "trace.json"
    artifact.save(_erroring_tool_trace(), src_path)

    res = runner.invoke(app, ["explain", "--backend", "ladybug", str(src_path), "A2"])

    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output
    assert "← plan" in res.output


def test_cli_query_can_use_ladybug_backend(tmp_path) -> None:
    a_path = tmp_path / "A.json"
    b_path = tmp_path / "B.json"
    artifact.save(_erroring_tool_trace(), a_path)
    artifact.save(_clean_trace(), b_path)

    res = runner.invoke(
        app,
        ["query", "tool-failure", "--backend", "ladybug", str(a_path), str(b_path)],
    )

    assert res.exit_code == 0, res.output
    assert "A" in res.output
    assert "call_tool" in res.output
    assert "1 match(es)" in res.output


def test_cli_query_ladybug_marquee_prints_honest_fallback_note(tmp_path) -> None:
    # The unbounded marquee can't compile under LadybugDB's cap, so --backend ladybug transparently
    # runs the pure-Python matcher. The CLI must SAY so (the project's honesty ethos) while
    # still returning the correct match — never silently pretend the accelerator ran it.
    p = tmp_path / "retry.json"
    artifact.save(_retry_trace(), p)
    res = runner.invoke(app, ["query", "tool-retry-failure", "--backend", "ladybug", str(p)])
    assert res.exit_code == 0, res.output
    assert "1 match(es)" in res.output  # correct result ...
    assert "not Cypher-compilable" in res.output  # ... plus the honest deferral note


def test_cli_query_ladybug_bounded_marquee_runs_native_no_fallback_note(tmp_path) -> None:
    # The bounded variant DOES compile, so the fallback note must NOT appear (no crying wolf).
    p = tmp_path / "retry.json"
    artifact.save(_retry_trace(), p)
    res = runner.invoke(app, ["query", "tool-retry-failure-near", "--backend", "ladybug", str(p)])
    assert res.exit_code == 0, res.output
    assert "1 match(es)" in res.output
    assert "not Cypher-compilable" not in res.output
