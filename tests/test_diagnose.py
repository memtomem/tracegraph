"""Automatic body-free failure diagnosis and behavior comparison."""

import json
from pathlib import Path

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
            origin=EdgeOrigin.SPAN_PARENT_FALLBACK,
        ),
        Edge(
            type=EdgeType.CAUSED_BY,
            src=f"{trace_id}-2",
            dst=f"{trace_id}-1",
            origin=EdgeOrigin.SPAN_PARENT_FALLBACK,
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
