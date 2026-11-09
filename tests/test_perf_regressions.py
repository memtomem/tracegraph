"""Wall-clock regression guards for the shapes that used to be quadratic.

Each test pins a path that was O(n²) (or worse) before the perf pass: per-step edge
rescans in normalize(), the unmemoized TREE_PARENT cycle walk, per-failure ancestor
recomputation in diagnose, and the sibling rescan in the logical-key builder. The
bounds are generous (CI machines vary) — a quadratic regression blows past them by an
order of magnitude, which is exactly what they're meant to catch.
"""

from __future__ import annotations

import gc
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
    for before, after in zip(timings, timings[1:], strict=False):
        assert after <= 3.5 * before + 0.05, timings


@pytest.mark.parametrize(
    "name_of",
    [lambda i: f"agent-{i}", lambda i: None, lambda i: f"agent-{i // 2}"],
    ids=["distinct-names", "null-names", "adjacent-pairs"],
)
def test_repeated_agent_failure_scales_linearly_when_names_rarely_repeat(name_of):
    # A chain of failing AGENT steps whose names never (or only adjacently) repeat. Before, the
    # unbounded two-step matcher walked and sorted every endpoint's full ancestor set before
    # filtering on the name back-reference: zero matches, yet ~1.3s at 2000 steps and 4x per
    # doubling.
    from tracegraph.analysis.patterns import PRESETS, find_matches

    timings = []
    for n in (2000, 4000, 8000):
        nt = normalize(RawTrace(
            trace=Trace(trace_id="t", source_kind="x"),
            steps=[
                Step(step_id=f"s{i}", trace_id="t", seq=i, name=name_of(i),
                     kind=StepKind.AGENT, status=StepStatus.ERROR)
                for i in range(n)
            ],
            causal_edges=[
                Edge(type=EdgeType.CAUSED_BY, src=f"s{i}", dst=f"s{i - 1}") for i in range(1, n)
            ],
        ))
        expected = sum(1 for i in range(1, n) if name_of(i) is not None and name_of(i) == name_of(i - 1))
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            assert len(find_matches(nt, PRESETS["repeated-agent-failure"])) == expected
            samples.append(time.perf_counter() - start)
        timings.append(median(samples))
    for before, after in zip(timings, timings[1:], strict=False):
        assert after <= 3 * before + 0.05, timings


def test_unbounded_two_step_does_not_rescan_a_high_fan_out_cause_per_endpoint():
    # One AGENT root causing n CHAIN steps, all of which pair with it. The longest-path pass
    # used to scan the root's n children once per endpoint: linear output, quadratic work.
    from tracegraph.analysis.patterns import PathPattern, StepPredicate, find_matches

    pattern = PathPattern(
        (StepPredicate(kind=StepKind.AGENT), StepPredicate(kind=StepKind.CHAIN, gap=(1, None)))
    )
    timings = []
    for n in (4000, 8000, 16000):
        steps = [Step(step_id="r", trace_id="t", seq=0, name="root", kind=StepKind.AGENT)]
        steps += [
            Step(step_id=f"c{i}", trace_id="t", seq=i, name=f"c{i}", kind=StepKind.CHAIN)
            for i in range(1, n)
        ]
        nt = normalize(RawTrace(
            trace=Trace(trace_id="t", source_kind="x"),
            steps=steps,
            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src=f"c{i}", dst="r") for i in range(1, n)],
        ))
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            assert len(find_matches(nt, pattern)) == n - 1
            samples.append(time.perf_counter() - start)
        timings.append(median(samples))
    for before, after in zip(timings, timings[1:], strict=False):
        assert after <= 3 * before + 0.05, timings


def test_otlp_loop_parent_resolution_scales_subquadratically():
    # A two-node loop ("agent" <-> "tool") where every span declares graph.node.id and
    # graph.node.parent_id. Before, _resolve_parent filtered and scanned all n/2 candidate
    # spans linearly for each of the n spans (O(n²)).
    from tracegraph.adapters import OTLPSpanAdapter

    # Doubling n leaves too little room between linear (2x) and quadratic (4x) growth: one GC
    # pause on a shared runner crossed a 3x bound. An 8x step separates them (8x vs 64x),
    # and the fastest of five runs with the collector off filters what noise remains.
    timings = []
    for n in (1000, 8000):
        spans = []
        for i in range(n):
            node = "agent" if i % 2 == 0 else "tool"
            parent_node = "tool" if i % 2 == 0 else "agent"
            attrs = [{"key": "graph.node.id", "value": {"stringValue": node}}]
            if i > 0:
                attrs.append(
                    {"key": "graph.node.parent_id", "value": {"stringValue": parent_node}}
                )
            spans.append(
                {
                    "traceId": "t",
                    "spanId": f"s{i:05d}",
                    "parentSpanId": "root" if i > 0 else "",
                    "name": node,
                    "startTimeUnixNano": str(1_700_000_000_000_000_000 + (i + 1) * 1000),
                    "endTimeUnixNano": str(1_700_000_000_000_000_000 + (i + 1) * 1000 + 500),
                    "attributes": attrs,
                }
            )
        spans[0]["spanId"] = "root"
        doc = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
        adapter = OTLPSpanAdapter(doc)
        samples = []
        gc.disable()
        try:
            for _ in range(5):
                start = time.perf_counter()
                raw = adapter.ingest("t")
                samples.append(time.perf_counter() - start)
                assert len(raw.steps) == n
        finally:
            gc.enable()
        timings.append(min(samples))
    assert timings[1] <= 24 * timings[0] + 0.05, timings


def test_langgraph_many_send_task_writes_scale_linearly(monkeypatch):
    # One fan-out checkpoint with n Send push tasks (1 failed + n-1 cancelled) and 50
    # candidate nodes in versions_seen/channel_versions. Before per-checkpoint caching,
    # _resolve_task_name recomputed _trigger_targets, _pull_task_ids, and _push_task_ids
    # from scratch for every task in writes (O(n²)).
    from types import SimpleNamespace
    from langgraph.types import Send
    from tracegraph.adapters import LangGraphCheckpointAdapter

    cid = "1f1ae6ea-e7d8-6c28-8001-03afbf35e17c"
    timings = []
    # The timing bound alone does not reject the uncached code at these sizes, so the
    # per-checkpoint helpers are also counted: uncached, both grow with n.
    helper_calls: dict[int, dict[str, int]] = {}
    real_targets = LangGraphCheckpointAdapter._trigger_targets
    real_push = LangGraphCheckpointAdapter._push_task_ids
    counts = {"_trigger_targets": 0, "_push_task_ids": 0}

    def counted_targets(cp):
        counts["_trigger_targets"] += 1
        return real_targets(cp)

    def counted_push(cls, cp, ns, md_step):
        counts["_push_task_ids"] += 1
        return real_push(cp, ns, md_step)
    for n in (150, 300, 600):
        sends = [Send("worker", {"i": i}) for i in range(n)]
        push_ids = LangGraphCheckpointAdapter._push_task_ids(
            {"id": cid, "v": 4, "channel_values": {"__pregel_tasks": sends}}, "", 1
        )
        task_ids = list(push_ids)
        writes = [(task_ids[0], "__error__", "RuntimeError('boom')")]
        writes.extend((tid, "__error__", "CancelledError()") for tid in task_ids[1:])
        cp = {
            "id": cid,
            "ts": "2026-01-01T00:00:00+00:00",
            "v": 4,
            "channel_values": {
                "__pregel_tasks": sends,
                **{f"branch:to:node_{k}": True for k in range(50)},
            },
            "channel_versions": {f"branch:to:node_{k}": 1 for k in range(50)},
            "versions_seen": {f"node_{k}": {} for k in range(50)},
        }
        tup = SimpleNamespace(
            config={"configurable": {"thread_id": "t", "checkpoint_ns": "", "checkpoint_id": cid}},
            checkpoint=cp,
            metadata={"source": "loop", "step": 1, "parents": {}},
            parent_config=None,
            pending_writes=writes,
        )
        saver = SimpleNamespace(list=lambda _cfg, t=tup: iter([t]))
        with monkeypatch.context() as m:
            m.setattr(LangGraphCheckpointAdapter, "_trigger_targets", staticmethod(counted_targets))
            m.setattr(LangGraphCheckpointAdapter, "_push_task_ids", classmethod(counted_push))
            counts.update(dict.fromkeys(counts, 0))
            assert len(LangGraphCheckpointAdapter(saver).ingest("t").steps) == n + 1
            helper_calls[n] = dict(counts)
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            raw = LangGraphCheckpointAdapter(saver).ingest("t")
            samples.append(time.perf_counter() - start)
            assert len(raw.steps) == n + 1
        timings.append(median(samples))
    assert len({tuple(c.values()) for c in helper_calls.values()}) == 1, helper_calls
    for before, after in zip(timings, timings[1:], strict=False):
        assert after <= 3 * before + 0.05, timings
