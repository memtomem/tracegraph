"""T2 CLI goldens over a real SyncMill FileTracer compete export."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.cli import app
from tracegraph.model import EdgeType, StepStatus


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures" / "syncmill"
REAL = FIXTURES / "real" / "compete.otlp.json"
REAL_SHA256 = "30a0069caa5f26489746158e5234b4a20e827345115a5299809bb6568855a626"
ALLOWED_KEYS = {
    "openinference.span.kind",
    "graph.node.id",
    "graph.node.parent_id",
    "syncmill.schema_version",
    "syncmill.run_id",
    "syncmill.strategy",
    "syncmill.phase",
    "syncmill.agent_id",
    "syncmill.attempt",
    "syncmill.status",
    "syncmill.exit_code",
    "syncmill.worktree_slot",
    "syncmill.winner",
    "syncmill.files_changed_count",
}
FORBIDDEN = re.compile(r"(?:prompt|patch|stdout|stderr|completion|api[_-]?key|password)", re.I)


def _spans(doc):
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _ingest(source: Path, target: Path):
    result = runner.invoke(app, ["ingest-otlp", "--file", str(source), "--out", str(target)])
    assert result.exit_code == 0, result.output
    return artifact.load(target)


def test_real_fixture_hash_allowlist_and_redaction():
    raw = REAL.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == REAL_SHA256
    doc = json.loads(raw)
    keys = {attr["key"] for span in _spans(doc) for attr in span["attributes"]}
    assert keys <= ALLOWED_KEYS
    assert not FORBIDDEN.search(raw.decode())
    assert all("events" not in span for span in _spans(doc))


def test_real_filetracer_ingest_inspect_and_lossy_select(tmp_path):
    target = tmp_path / "compete.json"
    trace = _ingest(REAL, target)
    assert trace.trace.run_id == "efd16fc1-758e-48fd-a765-bb3e27815952"
    result = runner.invoke(app, ["inspect", str(target)])
    assert result.exit_code == 0, result.output
    assert "select" in result.output and "lossy-projection" in result.output
    assert [step.name for step in trace.steps] == [
        "syncmill.run",
        "attempt:codex",
        "attempt:kimi-code",
        "select",
    ]
    select = next(step for step in trace.steps if step.name == "select")
    assert select.projection_lossy is True
    attempts = {step.step_id for step in trace.steps if step.name.startswith("attempt:")}
    caused = {(edge.src, edge.dst) for edge in trace.edges_of(EdgeType.CAUSED_BY)}
    assert not {(src, dst) for src, dst in caused if src in attempts and dst in attempts}


def test_explain_raw_compete_fan_in_timeout_and_gate_rejection(tmp_path):
    cases = [
        (REAL, "select", "attempt:codex"),
        (FIXTURES / "compete-timeout.otlp.json", "attempt:kimi-code", "timeout"),
        (FIXTURES / "compete-gate-reject.otlp.json", "gate:codex", "gate:"),
    ]
    compete_explain = ""
    for index, (source, name, expected) in enumerate(cases):
        target = tmp_path / f"case-{index}.json"
        trace = _ingest(source, target)
        step = next(step for step in trace.steps if step.name == name)
        if index:
            assert step.status is StepStatus.ERROR
        result = runner.invoke(app, ["explain", str(target), step.step_id])
        assert result.exit_code == 0, result.output
        assert expected in result.output
        if index == 0:
            compete_explain = result.output
    assert "raw chain" in compete_explain and "lossy" in compete_explain


def test_diff_controlled_rerun_identical_and_route_diverges(tmp_path):
    original = json.loads(REAL.read_text())
    rerun = deepcopy(original)
    spans = _spans(rerun)
    old_trace = spans[0]["traceId"]
    new_trace = "f" * 32
    span_map = {span["spanId"]: f"{index + 1:016x}" for index, span in enumerate(spans)}
    offset = 9_000_000_000
    for span in spans:
        span["traceId"] = new_trace
        span["spanId"] = span_map[span["spanId"]]
        if "parentSpanId" in span:
            span["parentSpanId"] = span_map[span["parentSpanId"]]
        for link in span.get("links", []):
            assert link["traceId"] == old_trace
            link["traceId"] = new_trace
            link["spanId"] = span_map[link["spanId"]]
        for key in ("startTimeUnixNano", "endTimeUnixNano"):
            span[key] = str(int(span[key]) + offset)
    rerun_path = tmp_path / "rerun.otlp.json"
    rerun_path.write_text(json.dumps(rerun))
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    route = tmp_path / "route.json"
    _ingest(REAL, a)
    _ingest(rerun_path, b)
    _ingest(FIXTURES / "route-success.otlp.json", route)
    same = runner.invoke(app, ["diff", str(a), str(b)])
    assert same.exit_code == 0, same.output
    assert "IDENTICAL" in same.output
    different = runner.invoke(app, ["diff", str(route), str(a)])
    assert different.exit_code == 1, different.output
    assert "NOT IDENTICAL" in different.output
