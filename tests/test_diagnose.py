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
