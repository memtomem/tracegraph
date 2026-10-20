"""Heuristic causal parents are disclosed by analyze and the incident memo, not only by check (#138).

The capture record says a parent was chosen heuristically somewhere in the trace, never which
one, so the memo stops calling any graph-parent edge a cause in such a trace, while declared
checkpoint parents and span links keep their wording.
"""

import json

from test_capture_provenance import gap_counts
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.adapters import OTLPSpanAdapter
from tracegraph.analysis import check
from tracegraph.analysis.diagnose import HEURISTIC_PARENT_FIDELITY_WARNING, analyze
from tracegraph.analysis.memo import build_incident_memo
from tracegraph.cli import app
from tracegraph.model import CAPTURE_GAP_CODES, HEURISTIC_EDGE_GAP_CODES
from tracegraph.normalize import normalize

runner = CliRunner()
DIGEST = "sha256:" + "0" * 64
WARNING_CLAUSE = HEURISTIC_PARENT_FIDELITY_WARNING[:1].lower() + HEURISTIC_PARENT_FIDELITY_WARNING[1:]
QUALIFIED = "preceded by (graph parent, possibly chosen heuristically)"


def _span(sid, start, *, parent="r", attrs=(), error=False, links=()):
    span = {"traceId": "t", "spanId": sid, "name": sid, "startTimeUnixNano": str(start),
            "status": {"code": 2 if error else 1},
            "attributes": [{"key": "openinference.span.kind", "value": {"stringValue": "CHAIN"}},
                           *({"key": k, "value": {"stringValue": v}} for k, v in attrs)]}
    if parent:
        span["parentSpanId"] = parent
    if links:
        span["links"] = [{"traceId": "t", "spanId": target} for target in links]
    return span


def _trace(spans):
    doc = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
    return normalize(OTLPSpanAdapter(doc).ingest("t"))


def _by_timing():
    """c names loop as its graph parent; a and b both carry it, so b is taken by start time. c
    also links to r, a declared cause."""
    t0 = 1_700_000_000_000_000_000
    return _trace([
        _span("r", t0, parent=None),
        _span("a", t0 + 100, attrs=[("graph.node.id", "loop")]),
        _span("b", t0 + 200, attrs=[("graph.node.id", "loop")]),
        _span("c", t0 + 300, attrs=[("graph.node.parent_id", "loop")], error=True, links=["r"]),
    ])


def _fallback_only():
    """c's graph parent resolves to nothing, so its span parent is used; d's resolves to one span."""
    t0 = 1_700_000_000_000_000_000
    return _trace([
        _span("r", t0, parent=None),
        _span("s", t0 + 50, attrs=[("graph.node.id", "solo")]),
        _span("c", t0 + 100, attrs=[("graph.node.parent_id", "missing")], error=True),
        _span("d", t0 + 200, attrs=[("graph.node.parent_id", "solo")], error=True),
    ])


def _plain():
    t0 = 1_700_000_000_000_000_000
    return _trace([_span("r", t0, parent=None), _span("c", t0 + 100, error=True)])


def _heuristic(report, label):
    return [w for w in report.warnings if w == f"{label}: {WARNING_CLAUSE}"]


def test_the_fixtures_carry_the_codes_they_are_named_for():
    assert gap_counts(_by_timing()) == {"otlp.graph_parent_by_timing": 1}
    assert gap_counts(_fallback_only()) == {"otlp.graph_parent_fallback": 1}
    assert gap_counts(_plain()) == {}


def test_analyze_warns_for_each_side_that_carries_a_heuristic_parent():
    assert len(_heuristic(analyze(_by_timing()), "current")) == 1
    report = analyze(_plain(), baseline=_fallback_only())
    assert _heuristic(report, "current") == [] and len(_heuristic(report, "baseline")) == 1
    report = analyze(_by_timing(), baseline=_fallback_only())
    assert len(_heuristic(report, "current")) == 1 and len(_heuristic(report, "baseline")) == 1
    assert not any(WARNING_CLAUSE in w for w in analyze(_plain(), baseline=_plain()).warnings)


def test_the_warning_comes_from_the_artifact_on_disk(tmp_path):
    path = tmp_path / "a.json"
    artifact.save(_by_timing(), path)
    out = tmp_path / "report.json"
    result = runner.invoke(app, ["analyze", str(path), "--json-out", str(out)])
    assert result.exit_code == 0, result.output
    assert f"current: {WARNING_CLAUSE}" in json.loads(out.read_text())["warnings"]


def _ancestry(memo):
    section = memo.split("### Causal Ancestry Graph\n", 1)[1].split("\n\n", 1)[0]
    return section.splitlines()


def test_the_memo_qualifies_graph_parents_only_where_one_was_timed():
    memo = build_incident_memo(_by_timing(), DIGEST)
    assert memo.count(f"- {HEURISTIC_PARENT_FIDELITY_WARNING}") == 1
    lines = _ancestry(memo)
    graph = [line for line in lines if "[origin: graph_parent]" in line]
    links = [line for line in lines if "[origin: span_link]" in line]
    assert graph and all(QUALIFIED in line for line in graph)
    assert links and all(" caused by " in line for line in links)
    assert not any("graph_parent]" in line and " caused by " in line for line in lines)


def test_a_fallback_alone_discloses_but_leaves_graph_parents_as_causes():
    memo = build_incident_memo(_fallback_only(), DIGEST)
    assert memo.count(f"- {HEURISTIC_PARENT_FIDELITY_WARNING}") == 1
    graph = [line for line in _ancestry(memo) if "[origin: graph_parent]" in line]
    assert graph and all(" caused by " in line for line in graph)
    assert not any(QUALIFIED in line for line in _ancestry(memo))


def test_a_trace_without_heuristic_parents_reads_as_before():
    memo = build_incident_memo(_plain(), DIGEST)
    assert HEURISTIC_PARENT_FIDELITY_WARNING not in memo and QUALIFIED not in memo


def test_one_code_set_serves_every_reader():
    assert HEURISTIC_EDGE_GAP_CODES <= CAPTURE_GAP_CODES
    assert check._HEURISTIC_EDGES is HEURISTIC_EDGE_GAP_CODES
