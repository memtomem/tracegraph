"""Phoenix CLI export -> body-free causal artifact."""

import json
from pathlib import Path

import pytest
from test_capture_provenance import gap_counts

from tracegraph import artifact
from tracegraph.adapters import PhoenixExportAdapter
from tracegraph.analysis.diagnose import analyze
from tracegraph.model import CausalFidelity, EdgeOrigin, EdgeType, StepKind, StepStatus
from tracegraph.normalize import normalize


def phoenix_trace():
    fixture = Path(__file__).parent / "fixtures" / "phoenix" / "trace-get-v1.8.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_phoenix_export_maps_parent_kind_status_and_metrics():
    raw = PhoenixExportAdapter(phoenix_trace()).ingest("trace-1")
    nt = normalize(raw)
    assert nt.trace.causal_fidelity is CausalFidelity.PARENT_ONLY
    assert nt.trace.links_preserved is False
    tool = nt.steps_by_id()["tool"]
    assert tool.kind is StepKind.TOOL and tool.status is StepStatus.ERROR
    assert tool.error_msg is None
    assert tool.evidence is not None
    assert tool.evidence.duration_ms == 500
    assert tool.evidence.total_tokens == 14
    assert tool.evidence.prompt_cost is None
    assert tool.evidence.total_cost == "0.0042"
    assert tool.evidence.evaluations[0].model_dump() == {
        "name": "correctness",
        "label": "fail",
        "score": 0.1,
    }
    (edge,) = nt.edges_of(EdgeType.CAUSED_BY)
    assert edge.origin is EdgeOrigin.SPAN_PARENT_FALLBACK
    metrics = analyze(nt).metrics
    assert metrics.total_tokens == 14
    assert metrics.total_cost == "0.0042"
    assert metrics.cost_currency == "USD"


def test_phoenix_artifact_is_body_free_and_records_what_it_withheld():
    nt = normalize(PhoenixExportAdapter(phoenix_trace()).ingest("trace-1"))
    text = artifact.dumps(nt)
    assert json.loads(text)["schema_version"] == 7
    # The prompt-shaped root name is dropped and the tool's status message is never kept;
    # both are recorded as unknown rather than left to look like an unnamed, message-less run.
    assert {s.step_id: s.unknown_fields for s in nt.steps} == {"root": ["name"], "tool": ["error_msg"]}
    assert gap_counts(nt) == {"otlp.unreadable_component_cost": 1}
    for forbidden in (
        "TOP SECRET",
        "PRIVATE RESULT",
        "password=secret",
        "private rubric text",
        "Alice's private salary",
        "input.value",
        "output.value",
        "SECRET TOKEN TEXT",
        "SECRET COST TEXT",
    ):
        assert forbidden not in text


def test_phoenix_array_discovery_and_trace_selection():
    other = {**phoenix_trace(), "traceId": "trace-2"}
    other["spans"] = [
        {**span, "context": {**span["context"], "trace_id": "trace-2"}}
        for span in other["spans"]
    ]
    adapter = PhoenixExportAdapter([phoenix_trace(), other])
    assert adapter.discover() == ["trace-1", "trace-2"]
    assert adapter.ingest("trace-2").trace.trace_id == "trace-2"


def test_phoenix_dangling_parent_and_mixed_trace_rejected():
    dangling = phoenix_trace()
    dangling["spans"][1]["parent_id"] = "missing"
    with pytest.raises(ValueError, match="partial/corrupt"):
        PhoenixExportAdapter(dangling).ingest("trace-1")

    mixed = phoenix_trace()
    mixed["spans"][1]["context"]["trace_id"] = "other"
    with pytest.raises(ValueError, match="another trace"):
        PhoenixExportAdapter(mixed)


def test_phoenix_naive_timestamp_normalizes_to_utc_across_timezones(monkeypatch):
    import os
    import time
    from tracegraph.adapters.phoenix_export import _nanos

    naive_iso = "2026-08-30T12:00:00.000000"
    expected = "1788091200000000000"
    orig_tz = os.environ.get("TZ")

    try:
        for tz in ("UTC", "Asia/Seoul", "America/New_York", "Europe/London"):
            monkeypatch.setenv("TZ", tz)
            if hasattr(time, "tzset"):
                time.tzset()
            assert _nanos(naive_iso) == expected
            assert _nanos("2026-08-30T12:00:00.000000Z") == expected
            assert _nanos("2026-08-30T12:00:00.000000+00:00") == expected
    finally:
        if orig_tz is not None:
            os.environ["TZ"] = orig_tz
        else:
            os.environ.pop("TZ", None)
        if hasattr(time, "tzset"):
            time.tzset()


def test_phoenix_artifact_drops_free_text_annotations():
    # The Phoenix import promises a body-free artifact. Span names were already filtered to
    # identifier-shaped text; annotation names and labels were only whitespace-collapsed and
    # truncated, so a label like "user said: my password is hunter2" reached the artifact.
    from tracegraph import artifact
    from tracegraph.normalize import normalize

    doc = {"traceId": "t", "spans": [{
        "context": {"trace_id": "t", "span_id": "s"}, "name": "agent", "status_code": "OK",
        "annotations": [
            {"name": "Customer Alice Smith complained", "result": {"label": "fail", "score": 0.1}},
            {"name": "correctness", "result": {"label": "user said: my password is hunter2", "score": 0.2}},
            {"name": "helpfulness", "result": {"label": "pass", "score": 0.9}},
        ],
    }]}
    nt = normalize(PhoenixExportAdapter(doc).ingest("t"))
    text = artifact.dumps(nt)
    assert "Alice" not in text and "hunter2" not in text
    evaluations = [(e.name, e.label, e.score) for e in nt.steps[0].evidence.evaluations]
    # A free-text name drops the whole evaluation (the name is its identity); a free-text
    # label drops only the label and keeps the score.
    assert evaluations == [("correctness", None, 0.2), ("helpfulness", "pass", 0.9)]
