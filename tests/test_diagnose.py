"""Automatic body-free failure diagnosis and behavior comparison."""

import json
from pathlib import Path

import pytest

from jsonschema import Draft202012Validator

from tracegraph.analysis.diagnose import analyze, dumps
from tracegraph.model import (
    CausalFidelity,
    DecisionEvidence,
    Edge,
    EdgeOrigin,
    EdgeType,
    RawTrace,
    Step,
    StepEvidence,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize


def _trace(trace_id: str, tool_status: StepStatus):
    steps = [
        Step(step_id=f"{trace_id}-0", trace_id=trace_id, seq=0, name="input"),
        Step(
            step_id=f"{trace_id}-1",
            trace_id=trace_id,
            seq=1,
            name="search",
            kind=StepKind.TOOL,
            status=tool_status,
            error_msg="password=secret",
        ),
        Step(
            step_id=f"{trace_id}-2",
            trace_id=trace_id,
            seq=2,
            name="agent",
            kind=StepKind.AGENT,
            status=tool_status,
            error_msg="propagated private output",
        ),
    ]
    edges = [
        Edge(
            type=EdgeType.CAUSED_BY,
            src=f"{trace_id}-1",
            dst=f"{trace_id}-0",
            origin=EdgeOrigin.GRAPH_PARENT,
        ),
        Edge(
            type=EdgeType.CAUSED_BY,
            src=f"{trace_id}-2",
            dst=f"{trace_id}-1",
            origin=EdgeOrigin.GRAPH_PARENT,
        ),
    ]
    return normalize(
        RawTrace(
            trace=Trace(
                trace_id=trace_id,
                source_kind="phoenix_cli",
                causal_fidelity=CausalFidelity.PARENT_ONLY,
                links_preserved=False,
            ),
            steps=steps,
            causal_edges=edges,
        )
    )


def test_primary_failure_is_lowest_error_and_report_is_body_free():
    report = analyze(_trace("bad", StepStatus.ERROR))
    assert [item.step.name for item in report.primary_failures] == ["search"]
    assert [item.name for item in report.propagated_failures] == ["agent"]
    assert report.warnings and "fan-in" in report.warnings[0]
    text = dumps(report)
    assert "password=secret" not in text
    assert "propagated private output" not in text


def test_comparison_catches_status_only_regression_without_topology_change():
    report = analyze(
        _trace("bad", StepStatus.ERROR),
        baseline=_trace("good", StepStatus.OK),
    )
    assert report.comparison is not None
    assert report.comparison.topology_identical
    assert {(item.name, item.before, item.after) for item in report.comparison.behavior_changes} == {
        ("search", "ok", "error"),
        ("agent", "ok", "error"),
    }


def test_success_report_has_no_failures_and_is_deterministic():
    nt = _trace("good", StepStatus.OK)
    first = dumps(analyze(nt))
    second = dumps(analyze(nt))
    assert first == second
    assert analyze(nt).primary_failures == []


def test_explicit_retry_marker_is_auto_diagnosed_as_version_two():
    trace_id = "retry-v2"
    steps = [
        Step(
            step_id=f"{trace_id}-0",
            trace_id=trace_id,
            seq=0,
            name="syncmill::probe",
            kind=StepKind.TOOL,
            status=StepStatus.OK,
        ),
        Step(
            step_id=f"{trace_id}-1",
            trace_id=trace_id,
            seq=1,
            name="retry:syncmill::probe",
            kind=StepKind.CHAIN,
            status=StepStatus.OK,
        ),
        Step(
            step_id=f"{trace_id}-2",
            trace_id=trace_id,
            seq=2,
            name="syncmill::probe",
            kind=StepKind.TOOL,
            status=StepStatus.ERROR,
        ),
    ]
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id=trace_id, source_kind="syncmill"),
            steps=steps,
            causal_edges=[
                Edge(type=EdgeType.CAUSED_BY, src=steps[1].step_id, dst=steps[0].step_id),
                Edge(type=EdgeType.CAUSED_BY, src=steps[2].step_id, dst=steps[1].step_id),
            ],
        )
    )

    retry_findings = [
        finding
        for finding in analyze(nt).patterns
        if finding.pattern_id == "tool-retry-failure"
    ]
    assert len(retry_findings) == 1
    assert retry_findings[0].pattern_version == 2
    assert retry_findings[0].step_ids == [step.step_id for step in steps]


def test_report_matches_public_json_schema():
    root = Path(__file__).parents[1]
    schema = json.loads((root / "contracts" / "analysis-report.schema.json").read_text())
    payload = json.loads(dumps(analyze(_trace("bad", StepStatus.ERROR))))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


def test_report_exposes_body_free_external_decision_evidence():
    nt = _trace("evidence", StepStatus.OK)
    nt.trace.decision_evidence = [
        DecisionEvidence(
            artifact_digest="sha256:" + "c" * 64,
            graph_generation=9,
            verdict="allow",
        )
    ]
    report = analyze(nt)
    assert report.schema_version == 2
    assert report.decision_evidence[0].model_dump() == {
        "source": "toolgraph_preflight",
        "artifact_digest": "sha256:" + "c" * 64,
        "graph_generation": 9,
        "verdict": "allow",
    }


def test_metrics_include_non_llm_evidence():
    nt = _trace("telemetry", StepStatus.OK)
    nt.steps[1].evidence = StepEvidence(
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        total_cost="0.0042",
        cost_currency="USD",
    )
    metrics = analyze(nt).metrics
    assert metrics.prompt_tokens == 10
    assert metrics.completion_tokens == 4
    assert metrics.total_tokens == 14
    assert metrics.total_cost == "0.0042"
    assert metrics.cost_currency == "USD"


def test_metrics_never_sum_mixed_or_partially_unknown_currencies():
    nt = _trace("mixed", StepStatus.OK)
    nt.steps[0].evidence = StepEvidence(total_cost="1.25", cost_currency="USD")
    nt.steps[1].evidence = StepEvidence(total_cost="2.50", cost_currency="EUR")
    metrics = analyze(nt).metrics
    assert metrics.total_cost is None
    assert metrics.cost_currency is None

    nt.steps[1].evidence = StepEvidence(total_cost="2.50")
    metrics = analyze(nt).metrics
    assert metrics.total_cost is None
    assert metrics.cost_currency is None


def test_comparison_only_subtracts_costs_with_the_same_known_currency():
    baseline = _trace("baseline-cost", StepStatus.OK)
    current = _trace("current-cost", StepStatus.OK)
    baseline.steps[0].evidence = StepEvidence(total_cost="1.25", cost_currency="USD")
    current.steps[0].evidence = StepEvidence(total_cost="2.50", cost_currency="USD")
    comparison = analyze(current, baseline=baseline).comparison
    assert comparison is not None
    assert comparison.metric_deltas["total_cost"] == "1.25"

    current.steps[0].evidence = StepEvidence(total_cost="2.50", cost_currency="EUR")
    comparison = analyze(current, baseline=baseline).comparison
    assert comparison is not None
    assert comparison.metric_deltas["total_cost"] is None

    current.steps[0].evidence = StepEvidence(total_cost="2.50")
    comparison = analyze(current, baseline=baseline).comparison
    assert comparison is not None
    assert comparison.metric_deltas["total_cost"] is None


def test_every_matching_preset_is_reported_not_just_the_first():
    """Presets describe the same steps from different angles; none may swallow another.

    A failing tool step is legitimately both ``tool-failure`` and ``error``. De-duplicating
    on the matched step tuple alone dropped whichever preset ran later, so a finding's
    presence depended on an unrelated preset having matched the same steps first.
    """
    nt = _trace("multi", StepStatus.ERROR)
    nt.steps[1].error_msg = "timeout waiting for upstream"
    by_preset = {}
    for finding in analyze(nt).patterns:
        by_preset.setdefault(finding.pattern_id, []).append(tuple(finding.step_ids))
    tool_step = (nt.steps[1].step_id,)
    assert tool_step in by_preset.get("tool-failure", []), by_preset
    assert tool_step in by_preset.get("error", []), by_preset


def test_pattern_deltas_are_independent_of_unrelated_presets():
    """A preset's count delta must not move because another preset matched the same steps."""
    baseline = _trace("delta-base", StepStatus.ERROR)
    current = _trace("delta-cur", StepStatus.ERROR)
    current.steps[1].error_msg = "timeout waiting for upstream"
    deltas = analyze(current, baseline=baseline).comparison.pattern_count_deltas
    # Only the timeout preset's count may change; the tool-failure/error findings exist on
    # both sides and must cancel out rather than being suppressed on one side only.
    assert deltas["tool-failure"] == 0, deltas
    assert deltas["error"] == 0, deltas


def test_negative_wall_duration_is_withheld_with_a_reason():
    nt = _trace("skewed", StepStatus.OK)
    nt.steps[0].ts = "2026-01-02T00:00:00Z"
    nt.steps[1].evidence = StepEvidence(end_ts="2026-01-01T00:00:00Z")
    report = analyze(nt)
    assert report.metrics.wall_duration_ms is None
    assert any("wall_duration_ms unavailable" in w for w in report.warnings), report.warnings


def test_partial_end_timestamp_coverage_is_disclosed():
    """Every step has a usable start, so only the end-coverage gap can raise this."""
    nt = _trace("partial", StepStatus.OK)
    for index, step in enumerate(nt.steps):
        step.ts = f"2026-01-01T00:00:0{index}Z"
    nt.steps[1].evidence = StepEvidence(end_ts="2026-01-01T00:00:02Z")
    report = analyze(nt)
    assert report.metrics.wall_duration_ms == 2000
    disclosure = next((w for w in report.warnings if "lower bound" in w), None)
    assert disclosure is not None, report.warnings
    assert "reported an end" in disclosure, disclosure
    assert "usable start" not in disclosure, disclosure


def test_unparseable_cost_withholds_the_total_instead_of_understating_it():
    """One corrupt cost must not produce a confident, knowably-too-low total."""
    nt = _trace("bad-cost", StepStatus.OK)
    nt.steps[0].evidence = StepEvidence(total_cost="1.00", cost_currency="USD")
    nt.steps[1].evidence = StepEvidence(total_cost="abc", cost_currency="USD")
    report = analyze(nt)
    assert report.metrics.total_cost is None
    assert any("total_cost unavailable" in w for w in report.warnings), report.warnings


def test_redaction_is_disclosed_only_when_an_alias_reaches_the_report():
    """A clean report must not carry the redaction disclosure.

    The flag used to be set by scanning every step name in the trace, including names that
    never reach any report field — so a report containing zero aliases still told the reader
    display text had been replaced.
    """
    # A successful trace: no failures, no pattern matches, no comparison — so this
    # natural-language step name is redacted by _safe() but never published anywhere.
    quiet = _trace("quiet", StepStatus.OK)
    quiet.steps[1].name = "search the user's private notes"
    report = analyze(quiet)
    disclosure = "deterministic redacted aliases"
    assert "redacted:" not in dumps(report)
    assert not any(disclosure in w for w in report.warnings), report.warnings

    nt = _trace("dirty", StepStatus.ERROR)
    nt.steps[1].name = "search the user's private notes"
    dirty = analyze(nt)
    assert any(disclosure in w for w in dirty.warnings), dirty.warnings
    assert "redacted:" in dumps(dirty)


def test_analyze_rejects_a_malformed_trace_with_value_error():
    """Library callers get ValueError, not a raw KeyError from inside a helper."""
    nt = _trace("malformed", StepStatus.OK)
    nt.edges.append(
        Edge(type=EdgeType.CAUSED_BY, src=nt.steps[0].step_id, dst="ghost-step")
    )
    with pytest.raises(ValueError):
        analyze(nt)


def test_analyze_rejects_a_malformed_baseline_with_value_error():
    baseline = _trace("bad-baseline", StepStatus.OK)
    baseline.edges.append(
        Edge(type=EdgeType.CAUSED_BY, src=baseline.steps[0].step_id, dst="ghost-step")
    )
    with pytest.raises(ValueError):
        analyze(_trace("fine", StepStatus.OK), baseline=baseline)


def test_missing_start_timestamps_are_disclosed_like_missing_ends():
    """A missing start distorts the duration exactly as a missing end does.

    The earliest step may be the one with no usable timestamp, in which case the computed
    span begins too late and the duration comes out short.
    """
    nt = _trace("no-start", StepStatus.OK)
    nt.steps[0].ts = None
    nt.steps[1].ts = "2026-01-01T00:00:01Z"
    nt.steps[2].ts = "2026-01-01T00:00:02Z"
    nt.steps[2].evidence = StepEvidence(end_ts="2026-01-01T00:00:03Z")
    report = analyze(nt)
    assert report.metrics.wall_duration_ms is not None
    assert any("usable start" in w for w in report.warnings), report.warnings


def test_baseline_metric_disclosures_reach_the_report():
    """A delta is only as trustworthy as both sides of it.

    The baseline's metrics were summarized with the notes discarded, so a comparison could
    publish a confident duration delta computed from a baseline number that, on its own,
    would have been reported as a lower bound.
    """
    baseline = _trace("bl", StepStatus.OK)
    baseline.steps[0].ts = "2026-01-01T00:00:00Z"
    baseline.steps[1].evidence = StepEvidence(end_ts="2026-01-01T00:00:02Z")

    current = _trace("cur", StepStatus.OK)
    for index, step in enumerate(current.steps):
        step.ts = f"2026-01-01T00:00:0{index}Z"
        step.evidence = StepEvidence(end_ts=f"2026-01-01T00:00:0{index + 1}Z")

    report = analyze(current, baseline=baseline)
    assert any(w.startswith("baseline: ") and "lower bound" in w for w in report.warnings), (
        report.warnings
    )
    # Disclosing that the baseline is a lower bound does not license subtracting it.
    assert report.comparison.metric_deltas["wall_duration_ms"] is None
    assert any("delta unavailable" in w for w in report.warnings), report.warnings


def _fully_timed(trace, start, end):
    for index, step in enumerate(trace.steps):
        step.ts = f"2026-01-01T00:00:0{start + index}Z"
    trace.steps[-1].evidence = StepEvidence(end_ts=f"2026-01-01T00:00:0{end}Z")
    for step in trace.steps[:-1]:
        step.evidence = StepEvidence(end_ts=f"2026-01-01T00:00:0{end}Z")
    return trace


def test_duration_delta_is_published_when_both_sides_are_complete():
    """The withholding must be targeted: two fully-observed traces still get a delta."""
    baseline = _fully_timed(_trace("bl-full", StepStatus.OK), 0, 3)
    current = _fully_timed(_trace("cur-full", StepStatus.OK), 0, 5)
    report = analyze(current, baseline=baseline)
    assert report.comparison.metric_deltas["wall_duration_ms"] == 2000
    assert not any("delta unavailable" in w for w in report.warnings), report.warnings


def test_duration_delta_is_withheld_when_the_current_side_is_partial():
    baseline = _fully_timed(_trace("bl-ok", StepStatus.OK), 0, 3)
    current = _fully_timed(_trace("cur-partial", StepStatus.OK), 0, 5)
    current.steps[1].evidence = None
    report = analyze(current, baseline=baseline)
    assert report.comparison.metric_deltas["wall_duration_ms"] is None
    assert any("delta unavailable" in w for w in report.warnings), report.warnings


def test_baseline_withheld_cost_is_disclosed():
    baseline = _trace("bl-cost", StepStatus.OK)
    baseline.steps[0].evidence = StepEvidence(total_cost="1.00", cost_currency="USD")
    baseline.steps[1].evidence = StepEvidence(total_cost="not-a-number", cost_currency="USD")
    report = analyze(_trace("cur-cost", StepStatus.OK), baseline=baseline)
    assert any("baseline: total_cost unavailable" in w for w in report.warnings), report.warnings


def test_a_step_ending_before_it_began_withholds_the_duration():
    """The aggregate envelope hides an internally inconsistent step.

    With steps spanning [0s, 2s] and [10s, 1s], `max(ends) - min(starts)` is a
    healthy-looking +2s and the 10s start never reaches the answer at all, so checking only
    the outer bounds reports a confident number built on contradictory timestamps.
    """
    nt = _trace("reversed", StepStatus.OK)
    nt.steps[0].ts = "2026-01-01T00:00:00Z"
    nt.steps[0].evidence = StepEvidence(end_ts="2026-01-01T00:00:02Z")
    nt.steps[1].ts = "2026-01-01T00:00:10Z"
    nt.steps[1].evidence = StepEvidence(end_ts="2026-01-01T00:00:01Z")
    nt.steps[2].ts = "2026-01-01T00:00:01Z"
    nt.steps[2].evidence = StepEvidence(end_ts="2026-01-01T00:00:02Z")

    report = analyze(nt)
    assert report.metrics.wall_duration_ms is None
    assert any("end before their own start" in w for w in report.warnings), report.warnings


def test_a_reversed_step_also_withholds_the_duration_delta():
    """An unusable duration must not become a confident comparison either."""
    baseline = _fully_timed(_trace("bl-rev", StepStatus.OK), 0, 3)
    current = _fully_timed(_trace("cur-rev", StepStatus.OK), 0, 5)
    current.steps[1].ts = "2026-01-01T00:00:10Z"
    report = analyze(current, baseline=baseline)
    assert report.metrics.wall_duration_ms is None
    assert report.comparison.metric_deltas["wall_duration_ms"] is None
    assert any("end before their own start" in w for w in report.warnings), report.warnings
