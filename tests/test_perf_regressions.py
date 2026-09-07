"""Wall-clock regression guards for the shapes that used to be quadratic.

Each test pins a path that was O(n²) (or worse) before the perf pass: per-step edge
rescans in normalize(), the unmemoized TREE_PARENT cycle walk, per-failure ancestor
recomputation in diagnose, and the sibling rescan in the logical-key builder. The
bounds are generous (CI machines vary) — a quadratic regression blows past them by an
order of magnitude, which is exactly what they're meant to catch.
"""

from __future__ import annotations

import time
from statistics import median

import pytest

from tracegraph.analysis.diagnose import analyze
from tracegraph.model import Edge, EdgeOrigin, EdgeType, RawTrace, Step, StepKind, StepStatus, Trace
from tracegraph.normalize import normalize, validate_normalized

pytestmark = pytest.mark.perf


def _deep_linear(n: int, *, error_every: int | None = None) -> RawTrace:
    steps = [
        Step(
            step_id=f"s{i}",
            trace_id="t",
            seq=i,
            name="tool-x",
            kind=StepKind.TOOL,
            status=(
                StepStatus.ERROR
                if error_every is not None and i and i % error_every == 0
                else StepStatus.OK
            ),
        )
        for i in range(n)
    ]
    edges = [
        Edge(type=EdgeType.CAUSED_BY, src=f"s{i}", dst=f"s{i - 1}", origin=EdgeOrigin.CHECKPOINT_PARENT) for i in range(1, n)
    ]
    return RawTrace(
        trace=Trace(trace_id="t", source_kind="x"), steps=steps, causal_edges=edges
    )


def _wide_fanout(n: int, *, error_every: int = 4) -> RawTrace:
    steps = [Step(step_id="root", trace_id="t", seq=0, name="root", kind=StepKind.AGENT)]
    edges = []
    for i in range(1, n):
        steps.append(
            Step(
                step_id=f"c{i}",
                trace_id="t",
                seq=i,
                name="tool-y",
                kind=StepKind.TOOL,
                status=StepStatus.ERROR if i % error_every == 0 else StepStatus.OK,
            )
        )
        edges.append(Edge(type=EdgeType.CAUSED_BY, src=f"c{i}", dst="root"))
    return RawTrace(
        trace=Trace(trace_id="t", source_kind="x"), steps=steps, causal_edges=edges
    )


def test_normalize_and_validate_are_linear_on_deep_linear_traces():
    # Before: _parents_of rescanned every edge per step and the cycle check walked the
    # full parent chain per node — ~3s at 6k steps. After: well under a second.
    raw = _deep_linear(6000)
    start = time.perf_counter()
    nt = normalize(raw)
    validate_normalized(nt)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"normalize+validate took {elapsed:.2f}s on a 6000-step chain"


def test_analyze_handles_many_errors_on_a_deep_chain():
    # Many primary/propagated failures on a deep chain: the per-failure edge re-sort and
    # double ancestor BFS used to make this O(F·E log E).
    nt = normalize(_deep_linear(3000, error_every=3))
    start = time.perf_counter()
    report = analyze(nt)
    elapsed = time.perf_counter() - start
    assert report.error_count == 999
    assert elapsed < 2.0, f"analyze took {elapsed:.2f}s on a 3000-step erroring chain"


def test_analyze_with_baseline_handles_wide_fanout():
    # Wide fan-out exercises the logical-key occurrence counter (was list.index per
    # sibling → O(n²)) and the baseline path (used to recompute patterns/metrics twice).
    nt = normalize(_wide_fanout(4000))
    start = time.perf_counter()
    report = analyze(nt, baseline=nt)
    elapsed = time.perf_counter() - start
    assert report.comparison is not None and report.comparison.topology_identical
    assert elapsed < 2.0, f"analyze+baseline took {elapsed:.2f}s on a 4000-child fan-out"


def test_deep_diff_scaling():
    from tracegraph.analysis.ahu import diff
    timings = []
    for n in (4000, 8000, 16000):
        a = normalize(_deep_linear(n))
        raw = _deep_linear(n)
        raw.steps[-1].name = "changed"
        b = normalize(raw)
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            assert not diff(a, b).identical
            samples.append(time.perf_counter() - start)
        timings.append(median(samples))
    for before, after in zip(timings, timings[1:]):
        assert after <= 3.5 * before + 0.05, timings
