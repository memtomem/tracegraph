"""Producer-owned qualified-tool telemetry through the real Tracegraph CLI."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.cli import app
from tracegraph.model import EdgeType, StepKind, StepStatus


runner = CliRunner()
FIXTURE = Path(__file__).parent / "fixtures" / "syncmill" / "live-qualified-tool.otlp.json"
FORBIDDEN = re.compile(r"prompt|patch|stdout|stderr|argument|result|api[_-]?key|password|secret", re.I)


def test_live_qualified_tool_ingest_query_and_export(tmp_path):
    normalized = tmp_path / "normalized.json"
    candidates = tmp_path / "candidates.json"

    ingested = runner.invoke(
        app, ["ingest-otlp", "--file", str(FIXTURE), "--out", str(normalized)]
    )
    assert ingested.exit_code == 0, ingested.output
    trace = artifact.load(normalized)
    tool = next(step for step in trace.steps if step.name == "gate_e::always_fail")
    gate = next(step for step in trace.steps if step.name == "gate:codex")
    attempt = next(step for step in trace.steps if step.name == "attempt:codex")
    assert tool.kind is StepKind.TOOL and tool.status is StepStatus.ERROR
    assert gate.kind is not StepKind.TOOL
    caused = {(edge.src, edge.dst) for edge in trace.edges_of(EdgeType.CAUSED_BY)}
    assert (tool.step_id, attempt.step_id) in caused

    queried = runner.invoke(app, ["query", "tool-failure", str(normalized)])
    assert queried.exit_code == 0, queried.output
    assert "gate_e::always_fail" in queried.output
    assert "gate:codex" not in queried.output

    exported = runner.invoke(
        app,
        ["export-review-candidates", "tool-failure", str(normalized), "--out", str(candidates)],
    )
    assert exported.exit_code == 0, exported.output
    report = json.loads(candidates.read_text(encoding="utf-8"))
    assert len(report["candidates"]) == 1
    assert report["candidates"][0] == {
        "run_id": "gate-e-live-001",
        "pattern_id": "tool-failure",
        "pattern_version": 1,
        "tool_key": "gate_e::always_fail",
        "artifact_digest": f"sha256:{hashlib.sha256(normalized.read_bytes()).hexdigest()}",
    }


def test_live_qualified_fixture_is_body_free_and_digest_stable():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "744b6ede10e9fbe5dc7d9c834f63f5eacba5c05684225546a8cb8e3c412c383b"
    )
    assert FORBIDDEN.search(raw.decode("utf-8")) is None
    doc = json.loads(raw)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert all("events" not in span for span in spans)
