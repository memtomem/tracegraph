"""Phoenix CLI export -> body-free causal artifact."""

import json
from pathlib import Path

import pytest

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


def test_phoenix_artifact_is_body_free_and_v2():
    text = artifact.dumps(normalize(PhoenixExportAdapter(phoenix_trace()).ingest("trace-1")))
    assert json.loads(text)["schema_version"] == 2
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
