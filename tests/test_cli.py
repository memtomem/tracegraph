"""CLI smoke test: ingest a real SqliteSaver DB, then inspect / explain / diff."""

import builtins
import json
import os
import re
import subprocess
from types import SimpleNamespace
import sys

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
import typer
from tiny_agent import run
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph import cli as cli_mod
from tracegraph.cli import app
from tracegraph.model import (
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepEvidence,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

runner = CliRunner()


def _phoenix_export(trace_id="phoenix-1", *, status="ERROR"):
    return {
        "traceId": trace_id,
        "status": status,
        "spans": [
            {
                "id": "root",
                "context": {"trace_id": trace_id, "span_id": "root"},
                "name": "agent",
                "span_kind": "AGENT",
                "start_time": "2026-07-14T00:00:00Z",
                "end_time": "2026-07-14T00:00:01Z",
                "status_code": status,
                "status_message": "password=secret",
                "attributes": {"input.value": "private prompt"},
            }
        ],
    }


def _write_linear_trace(path, trace_id, *specs):
    """Save a linear (each step caused by the previous) NormalizedTrace under ``path``.

    Each ``specs`` entry is ``(name, kind, status)``. Used to construct CLI fixtures
    with shapes that the real ``tiny_agent`` can't easily reproduce — e.g. multiple
    independent failing traces for exercising ``query --limit``.
    """
    steps, edges = [], []
    for i, (name, kind, status) in enumerate(specs):
        steps.append(
            Step(
                step_id=f"{trace_id}{i}",
                trace_id=trace_id,
                seq=i,
                name=name,
                kind=kind,
                status=status,
            )
        )
        if i:
            edges.append(
                Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}{i}", dst=f"{trace_id}{i - 1}")
            )
    nt = normalize(
        RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"), steps=steps, causal_edges=edges)
    )
    artifact.save(nt, path)


@pytest.fixture
def artifacts(tmp_path):
    """Populate a sqlite DB with runs A (error) and B (ok); ingest both to artifacts."""
    db = tmp_path / "trace.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        run(saver, "A", "boom-please")
        run(saver, "B", "hello")

    a_json, b_json = tmp_path / "A.json", tmp_path / "B.json"
    for thread, out in [("A", a_json), ("B", b_json)]:
        res = runner.invoke(app, ["ingest", "--sqlite", str(db), "--thread", thread, "--out", str(out)])
        assert res.exit_code == 0, res.output
    return a_json, b_json


def test_inspect_renders_error_step(artifacts):
    a_json, _ = artifacts
    res = runner.invoke(app, ["inspect", str(a_json)])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output
    assert "error" in res.output.lower()


def test_validate_accepts_artifact_files_and_directories(artifacts):
    a_json, _ = artifacts
    res = runner.invoke(app, ["validate", str(a_json), str(a_json.parent)])
    assert res.exit_code == 0, res.output
    assert "OK" in res.output
    assert "3 artifact(s) valid" in res.output


def test_validate_reports_corrupt_artifact(tmp_path):
    valid = tmp_path / "valid.json"
    corrupt = tmp_path / "corrupt.json"
    _write_linear_trace(
        valid,
        "V",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("call_tool", StepKind.TOOL, StepStatus.ERROR),
    )
    payload = json.loads(valid.read_text(encoding="utf-8"))
    payload["trace"]["edges"] = [
        e for e in payload["trace"]["edges"] if e["type"] != EdgeType.TREE_PARENT.value
    ]
    corrupt.write_text(json.dumps(payload), encoding="utf-8")

    res = runner.invoke(app, ["validate", str(valid), str(corrupt)])
    assert res.exit_code == 1, res.output
    assert "OK" in res.output
    assert "INVALID" in res.output
    assert "canonical form" in res.output
    assert "1 invalid artifact(s)" in res.output


def test_ingest_phoenix_and_analyze_are_body_free(tmp_path):
    source = tmp_path / "phoenix.json"
    target = tmp_path / "artifact.json"
    report = tmp_path / "report.json"
    source.write_text(json.dumps(_phoenix_export()), encoding="utf-8")

    ingested = runner.invoke(
        app, ["ingest-phoenix", "--file", str(source), "--out", str(target)]
    )
    assert ingested.exit_code == 0, ingested.output
    analyzed = runner.invoke(app, ["analyze", str(target), "--json-out", str(report)])
    assert analyzed.exit_code == 0, analyzed.output
    assert "What failed" in analyzed.output
    assert "parent_only" in analyzed.output
    combined = target.read_text(encoding="utf-8") + report.read_text(encoding="utf-8")
    assert "password=secret" not in combined
    assert "private prompt" not in combined


def test_analyze_accepts_phoenix_stdin():
    result = runner.invoke(app, ["analyze", "-"], input=json.dumps(_phoenix_export()))
    assert result.exit_code == 0, result.output
    assert "phoenix-1" in result.output and "What failed" in result.output


def test_phoenix_diagnose_uses_read_only_px_and_writes_safe_outputs(tmp_path, monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.8.1\n")
        if command[1:3] == ["trace", "get"]:
            payload = _phoenix_export()
            payload["spans"][0]["annotations"] = [{"name": "quality", "score": 0.5}]
            return SimpleNamespace(stdout=json.dumps(payload))
        raise AssertionError(f"unexpected px command: {command}")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    artifact_path = tmp_path / "safe.json"
    report_path = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "phoenix",
            "diagnose",
            "phoenix-1",
            "--save-artifact",
            str(artifact_path),
            "--json-out",
            str(report_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0][0] == ["px", "--version"]
    assert seen[1][0][:4] == ["px", "trace", "get", "phoenix-1"]
    assert "--include-annotations" in seen[1][0]
    assert seen[1][1]["check"] is True
    assert all(command[1] not in {"annotate", "add-note", "delete"} for command, _ in seen)
    assert artifact_path.exists() and report_path.exists()
    assert "password=secret" not in artifact_path.read_text(encoding="utf-8")
    assert json.loads(report_path.read_text())["metrics"]["evaluations"] == [
        {
            "step_id": "root",
            "name": "quality",
            "label": None,
            "score": 0.5,
        }
    ]


def test_phoenix_diagnose_missing_px_is_actionable(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("px")

    monkeypatch.setattr(cli_mod.subprocess, "run", missing)
    result = runner.invoke(app, ["phoenix", "diagnose", "t"])
    assert result.exit_code != 0
    assert "Phoenix CLI `px` was not found" in result.output


def test_phoenix_diagnose_auto_selects_latest_error_and_forwards_project(monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="@arizeai/phoenix-cli 1.8.1")
        if command[1:3] == ["trace", "list"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        _phoenix_export("latest-ok", status="OK"),
                        _phoenix_export("latest-error", status="ERROR"),
                    ]
                )
            )
        raise AssertionError(f"unexpected px command: {command}")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose", "--project", "agent-prod"])
    assert result.exit_code == 0, result.output
    assert "selected latest failed Phoenix trace" in result.output
    assert "latest-error" in result.output
    assert seen[1][1:3] == ["trace", "list"]
    assert "--include-annotations" in seen[1]
    assert seen[1][-2:] == ["--project", "agent-prod"]
    assert not any(command[1:3] == ["trace", "get"] for command in seen)


def test_phoenix_diagnose_forwards_project_to_trace_and_baseline(monkeypatch):
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.8.1")
        if command[1:3] == ["trace", "get"]:
            return SimpleNamespace(stdout=json.dumps(_phoenix_export(command[3], status="OK")))
        raise AssertionError(f"unexpected px command: {command}")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "phoenix",
            "diagnose",
            "current",
            "--baseline",
            "baseline",
            "--project",
            "agent-prod",
        ],
    )
    assert result.exit_code == 0, result.output
    get_commands = [command for command in seen if command[1:3] == ["trace", "get"]]
    assert [command[3] for command in get_commands] == ["current", "baseline"]
    assert all(command[-2:] == ["--project", "agent-prod"] for command in get_commands)


def test_phoenix_diagnose_falls_back_to_newest_trace(monkeypatch):
    def fake_run(command, **kwargs):
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.8.1")
        return SimpleNamespace(
            stdout=json.dumps(
                [_phoenix_export("newest", status="OK"), _phoenix_export("older", status="OK")]
            )
        )

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose"])
    assert result.exit_code == 0, result.output
    assert "No failed trace found in the latest 2" in result.output
    assert "newest" in result.output


def test_phoenix_diagnose_empty_project_is_actionable(monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(stdout="1.8.1" if command == ["px", "--version"] else "[]")

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose"])
    assert result.exit_code != 0
    assert "configured Phoenix project has no traces" in result.output


def test_phoenix_diagnose_rejects_trace_list_without_aggregate_status(monkeypatch):
    payload = _phoenix_export("missing-status")
    payload.pop("status")

    def fake_run(command, **kwargs):
        if command == ["px", "--version"]:
            return SimpleNamespace(stdout="1.8.1")
        return SimpleNamespace(stdout=json.dumps([payload]))

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose"])
    assert result.exit_code != 0
    assert "required" in result.output and "status" in result.output


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (subprocess.TimeoutExpired(["px"], 60), "timed out"),
        (subprocess.CalledProcessError(1, ["px"], stderr="password=secret"), "failed with exit 1"),
    ],
)
def test_phoenix_diagnose_sanitizes_px_process_failures(monkeypatch, failure, expected):
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(stdout="1.8.1")
        raise failure

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code != 0
    assert expected in result.output
    assert "password=secret" not in result.output


def test_phoenix_diagnose_rejects_malformed_px_json(monkeypatch):
    responses = iter(["1.8.1", "not-json"])
    monkeypatch.setattr(
        cli_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code != 0
    assert "returned invalid JSON" in result.output


@pytest.mark.parametrize("version", ["0.9.9", "unknown"])
def test_phoenix_diagnose_rejects_unsupported_px_versions(monkeypatch, version):
    monkeypatch.setattr(
        cli_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=version),
    )
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code != 0
    assert "1.0.4" in result.output


def test_phoenix_doctor_reports_ready_warn_and_fail(monkeypatch):
    responses = iter(["1.8.1", "[]", json.dumps([_phoenix_export(status="OK")])])
    monkeypatch.setattr(
        cli_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    ready = runner.invoke(app, ["phoenix", "doctor", "--project", "agent-prod"])
    assert ready.exit_code == 0, ready.output
    assert "READY" in ready.output and "agent-prod" in ready.output

    responses = iter(["1.8.1", "[]", "[]"])
    monkeypatch.setattr(
        cli_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    warning = runner.invoke(app, ["phoenix", "doctor"])
    assert warning.exit_code == 0, warning.output
    assert "WARN" in warning.output and "no traces yet" in warning.output

    monkeypatch.setattr(
        cli_mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("px")),
    )
    failed = runner.invoke(app, ["phoenix", "doctor"])
    assert failed.exit_code == 1
    assert "FAIL" in failed.stderr and "px` was not found" in failed.stderr


def test_phoenix_diagnose_crosses_real_subprocess_boundary(tmp_path, monkeypatch):
    payload = _phoenix_export("subprocess-trace")
    payload["spans"][0]["annotations"] = [{"name": "quality", "label": "fail"}]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_px = bin_dir / "px"
    log = tmp_path / "px.log"
    fake_px.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_PX_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('1.8.1')\n"
        "elif sys.argv[1:3] == ['trace', 'list']:\n"
        f"    print({json.dumps([payload])!r})\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    fake_px.chmod(0o755)
    monkeypatch.setenv("FAKE_PX_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    safe = tmp_path / "safe.json"
    report = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "phoenix",
            "diagnose",
            "--save-artifact",
            str(safe),
            "--json-out",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    combined = result.output + safe.read_text() + report.read_text()
    assert "private prompt" not in combined
    assert "password=secret" not in combined
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0] == ["--version"]
    assert commands[1][:2] == ["trace", "list"]
    assert "--include-annotations" in commands[1]


def test_analyze_renders_baseline_telemetry_deltas(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write_linear_trace(baseline, "baseline", ("agent", StepKind.AGENT, StepStatus.OK))
    _write_linear_trace(current, "current", ("agent", StepKind.AGENT, StepStatus.OK))
    baseline_trace = artifact.load(baseline)
    baseline_trace.steps[0].evidence = StepEvidence(
        total_tokens=10, total_cost="1.00", cost_currency="USD"
    )
    artifact.save(baseline_trace, baseline)
    current_trace = artifact.load(current)
    current_trace.steps[0].evidence = StepEvidence(
        total_tokens=16, total_cost="1.15", cost_currency="USD"
    )
    artifact.save(current_trace, current)

    result = runner.invoke(app, ["analyze", str(current), "--baseline", str(baseline)])
    assert result.exit_code == 0, result.output
    assert "telemetry delta:" in result.output
    assert "tokens=+6" in result.output
    assert "cost=+0.15 USD" in result.output


def test_explain_shows_causal_chain(artifacts):
    a_json, _ = artifacts
    store_res = runner.invoke(app, ["inspect", str(a_json)])
    assert store_res.exit_code == 0
    # explain the call_tool error step: find its id via the artifact
    from tracegraph import artifact

    nt = artifact.load(a_json)
    err = next(s for s in nt.steps if s.status.value == "error")
    res = runner.invoke(app, ["explain", str(a_json), err.step_id])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output
    assert "←" in res.output  # the causal chain arrow


def test_diff_detects_divergence(artifacts):
    a_json, b_json = artifacts
    res = runner.invoke(app, ["diff", str(a_json), str(b_json)])
    assert res.exit_code == 1, res.output  # non-zero so CI can gate on regressions
    assert "NOT IDENTICAL" in res.output


def test_diff_identical_for_same_run(artifacts):
    a_json, _ = artifacts
    res = runner.invoke(app, ["diff", str(a_json), str(a_json)])
    assert res.exit_code == 0, res.output
    assert "IDENTICAL" in res.output


def test_query_finds_failure_in_only_one_trace(artifacts):
    a_json, b_json = artifacts
    res = runner.invoke(app, ["query", "tool-failure", str(a_json), str(b_json)])
    assert res.exit_code == 0, res.output
    assert "A" in res.output and "call_tool" in res.output
    # B (the ok run) must not appear as a match line
    assert "1 match(es)" in res.output


def test_query_directory_and_no_match(artifacts):
    a_json, _ = artifacts
    folder = a_json.parent
    res = runner.invoke(app, ["query", "plan-then-tool-failure", str(folder)])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output


def test_query_unknown_preset_errors(artifacts):
    a_json, _ = artifacts
    res = runner.invoke(app, ["query", "nope", str(a_json)])
    assert res.exit_code != 0
    assert "unknown preset" in res.output


def test_presets_lists_patterns():
    res = runner.invoke(app, ["presets"])
    assert res.exit_code == 0
    assert "tool-failure" in res.output


def _write_retry_trace(path, trace_id="RT"):
    """input → plan → search(ok) → explicit retry marker → search(error)."""
    specs = [
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("plan", StepKind.CHAIN, StepStatus.OK),
        ("search", StepKind.TOOL, StepStatus.OK),
        ("retry:search", StepKind.CHAIN, StepStatus.OK),
        ("search", StepKind.TOOL, StepStatus.ERROR),
    ]
    steps, edges = [], []
    for i, (name, kind, status) in enumerate(specs):
        steps.append(Step(step_id=f"{trace_id}{i}", trace_id=trace_id, seq=i, name=name,
                          kind=kind, status=status))
        if i:
            edges.append(Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}{i}", dst=f"{trace_id}{i - 1}"))
    nt = normalize(
        RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"), steps=steps, causal_edges=edges)
    )
    artifact.save(nt, path)


def test_presets_lists_marquee_retry_pattern():
    res = runner.invoke(app, ["presets"])
    assert res.exit_code == 0
    assert "tool-retry-failure" in res.output
    assert "retry:" in res.output and "name=#0" in res.output


def test_query_marquee_finds_repeated_failing_tool(tmp_path):
    p = tmp_path / "retry.json"
    _write_retry_trace(p)
    res = runner.invoke(app, ["query", "tool-retry-failure", str(p)])
    assert res.exit_code == 0, res.output
    # both 'search' invocations appear in the match line
    assert res.output.count("search") >= 2
    assert "1 match(es)" in res.output


def test_query_marquee_no_match_on_single_clean_tool(tmp_path):
    # A trace with one successful tool and no repeat must NOT match — exit 1, "no matches".
    p = tmp_path / "clean.json"
    _write_linear_trace(
        p,
        "CLEAN",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("call_tool", StepKind.TOOL, StepStatus.OK),
    )
    res = runner.invoke(app, ["query", "tool-retry-failure", str(p)])
    assert res.exit_code == 1
    assert "no matches" in res.output


def test_ladybug_backend_missing_optional_dependency_reports_bad_parameter(monkeypatch):
    monkeypatch.delitem(sys.modules, "tracegraph.store.ladybug", raising=False)
    monkeypatch.delitem(sys.modules, "ladybug", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ladybug":
            raise ModuleNotFoundError("No module named 'ladybug'", name="ladybug")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(typer.BadParameter, match=r"tracegraph\[cypher\]"):
        cli_mod._store_cls(cli_mod.QueryBackend.LADYBUG)


def test_ladybug_backend_broken_import_is_not_hidden(monkeypatch):
    monkeypatch.delitem(sys.modules, "tracegraph.store.ladybug", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tracegraph.store.ladybug":
            raise ImportError("backend broken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match="backend broken"):
        cli_mod._store_cls(cli_mod.QueryBackend.LADYBUG)


# --- query --limit / --explain ------------------------------------------------------
#
# These fixtures bypass the LangGraph SqliteSaver round-trip and write artifacts
# directly, because the ergonomics flags need multi-trace scenarios that tiny_agent
# can't produce on its own (it's a single linear graph).


@pytest.fixture
def three_failing_traces(tmp_path):
    """Three independent traces, each with input → plan → call_tool(error)."""
    paths = []
    for tid in ("T0", "T1", "T2"):
        p = tmp_path / f"{tid}.json"
        _write_linear_trace(
            p,
            tid,
            ("input", StepKind.CHAIN, StepStatus.OK),
            ("plan", StepKind.CHAIN, StepStatus.OK),
            ("call_tool", StepKind.TOOL, StepStatus.ERROR),
        )
        paths.append(p)
    return paths


def test_query_limit_truncates_to_n_and_notes_total(three_failing_traces):
    res = runner.invoke(
        app,
        ["query", "tool-failure", "--limit", "2", *map(str, three_failing_traces)],
    )
    assert res.exit_code == 0, res.output
    assert res.output.count("call_tool") == 2  # only 2 match lines, not 3
    assert "T2" not in res.output  # tail trace was truncated
    assert "2 match(es)" in res.output
    assert "truncated from 3" in res.output


def test_query_limit_higher_than_total_shows_all_with_no_truncation_note(
    three_failing_traces,
):
    res = runner.invoke(
        app,
        ["query", "tool-failure", "--limit", "99", *map(str, three_failing_traces)],
    )
    assert res.exit_code == 0, res.output
    assert res.output.count("call_tool") == 3
    assert "3 match(es)" in res.output
    assert "truncated" not in res.output


def test_query_limit_zero_rejected(three_failing_traces):
    res = runner.invoke(
        app,
        ["query", "tool-failure", "--limit", "0", *map(str, three_failing_traces)],
    )
    assert res.exit_code != 0
    assert "must be a positive integer" in res.output


def test_query_limit_negative_rejected(three_failing_traces):
    res = runner.invoke(
        app,
        ["query", "tool-failure", "--limit", "-1", *map(str, three_failing_traces)],
    )
    assert res.exit_code != 0
    assert "must be a positive integer" in res.output


def test_query_explain_renders_ancestor_chain(three_failing_traces):
    # The pattern matches the call_tool error step; --explain must walk back through
    # plan and input (the raw causal ancestors of that effect).
    one = three_failing_traces[0]
    res = runner.invoke(app, ["query", "tool-failure", "--explain", str(one)])
    assert res.exit_code == 0, res.output
    # Match line is present:
    assert "call_tool" in res.output
    # And the ancestor chain renders backward (call_tool ← plan ← input):
    assert "← plan" in res.output
    assert "← input" in res.output


def test_query_explain_on_root_match_says_no_causes(tmp_path):
    # A single-step trace where the only step is an error — the match's effect IS the
    # root, so explain should tell the user that explicitly (rather than printing
    # nothing, which would read as a bug).
    p = tmp_path / "root.json"
    _write_linear_trace(p, "R", ("only", StepKind.CHAIN, StepStatus.ERROR))
    res = runner.invoke(app, ["query", "error", "--explain", str(p)])
    assert res.exit_code == 0, res.output
    assert "no causes" in res.output


def test_query_rejects_duplicate_trace_ids_across_artifacts(tmp_path):
    # Two artifacts claiming the same trace_id are ambiguous: --explain looks up the
    # originating trace by id, so silently keeping the last-loaded copy would attach
    # matches from one file to another's causal graph. Fail loudly at load time —
    # `query` (and any future command using `_load_many`) gets the guard for free.
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    _write_linear_trace(
        a,
        "DUP",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("plan", StepKind.CHAIN, StepStatus.ERROR),
    )
    _write_linear_trace(
        b,
        "DUP",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("plan", StepKind.CHAIN, StepStatus.OK),
    )
    res = runner.invoke(app, ["query", "error", str(a), str(b)])
    assert res.exit_code != 0
    assert "duplicate trace_id" in res.output


def test_query_explain_renders_lossy_warning_for_fan_in_match(tmp_path):
    # When the matched effect (or any chain step) has multiple real causes — the
    # raw/derived split's whole point — --explain must surface the same warning
    # `tracegraph explain` prints, otherwise the tree-projection's lossiness becomes
    # invisible and the repo's causal-honesty contract is silently broken.
    p = tmp_path / "fanin.json"
    steps = [
        Step(step_id="r0", trace_id="R", seq=0, name="input", kind=StepKind.CHAIN),
        Step(step_id="r1", trace_id="R", seq=1, name="plan_a", kind=StepKind.CHAIN),
        Step(step_id="r2", trace_id="R", seq=2, name="plan_b", kind=StepKind.CHAIN),
        Step(
            step_id="r3",
            trace_id="R",
            seq=3,
            name="merge",
            kind=StepKind.TOOL,
            status=StepStatus.ERROR,
        ),
    ]
    edges = [
        Edge(type=EdgeType.CAUSED_BY, src="r1", dst="r0"),
        Edge(type=EdgeType.CAUSED_BY, src="r2", dst="r0"),
        Edge(type=EdgeType.CAUSED_BY, src="r3", dst="r1"),
        Edge(type=EdgeType.CAUSED_BY, src="r3", dst="r2"),
    ]
    nt = normalize(
        RawTrace(trace=Trace(trace_id="R", source_kind="x"), steps=steps, causal_edges=edges)
    )
    artifact.save(nt, p)
    res = runner.invoke(app, ["query", "tool-failure", "--explain", str(p)])
    assert res.exit_code == 0, res.output
    # The matched effect "merge" has 2 real causes -> result.is_lossy == True. The copy
    # ("multiple real causes") is the same load-bearing phrase `tracegraph explain` uses.
    assert "multiple real causes" in res.output


def test_query_explain_skips_lossy_warning_when_match_has_single_cause_chain(
    three_failing_traces,
):
    # Negative guard for the test above: a linear (non-fan-in) match must NOT print
    # the warning. Otherwise we'd be crying wolf and the signal becomes meaningless.
    one = three_failing_traces[0]
    res = runner.invoke(app, ["query", "tool-failure", "--explain", str(one)])
    assert res.exit_code == 0, res.output
    assert "multiple real causes" not in res.output


def test_query_explain_with_limit_only_explains_shown_matches(three_failing_traces):
    # --limit must apply before --explain so we never spend on rendering ancestors for
    # matches the user asked us to hide.
    res = runner.invoke(
        app,
        [
            "query",
            "tool-failure",
            "--limit",
            "1",
            "--explain",
            *map(str, three_failing_traces),
        ],
    )
    assert res.exit_code == 0, res.output
    assert res.output.count("call_tool") == 1  # one match line
    assert "← plan" in res.output  # explain rendered for it
    assert "T1" not in res.output and "T2" not in res.output  # other traces hidden
    assert "truncated from 3" in res.output


# --- Tier-2 robustness: clean load errors, lenient directory globbing, exact explain ids ---


def _panel_text(output: str) -> str:
    """Collapse typer's Rich error-panel escapes + box-drawing + wrapping into one searchable string.

    typer renders BadParameter inside a panel that hard-wraps at the console width, inserting
    ``│``, newlines and padding mid-message — so a long (path-bearing) message can split an
    asserted phrase across the border (even ``"cannot load artifact"`` at a very narrow width).
    Under a color-forcing terminal Rich *also* injects ANSI escapes that would sit between the
    split words. ``conftest.py`` forces ``TERM=dumb`` so color is normally off, but we still
    strip escapes here defensively (in case a module is run without the conftest): OSC
    sequences (e.g. hyperlinks ``\\x1b]8;;…``) then CSI/SGR sequences (``\\x1b[31m`` …). We then
    collapse all whitespace/box runs to single spaces — Rich wraps at word boundaries, so this
    reconstitutes any phrase regardless of color, path length, or terminal width.
    """
    no_osc = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", output)  # OSC ... BEL/ST
    no_ansi = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", no_osc)  # CSI (incl. SGR)
    return re.sub(r"[\s│╭╮╰╯─]+", " ", no_ansi)


def test_inspect_missing_file_reports_clean_error(tmp_path):
    # A nonexistent artifact must produce a clean CLI error, not a raw FileNotFoundError
    # traceback. The clean message (only emitted via typer.BadParameter) proves we caught it.
    res = runner.invoke(app, ["inspect", str(tmp_path / "nope.json")])
    assert res.exit_code != 0
    assert "cannot load artifact" in _panel_text(res.output)
    assert "file not found" in _panel_text(res.output)


def test_inspect_malformed_json_reports_clean_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not valid json ", encoding="utf-8")
    res = runner.invoke(app, ["inspect", str(bad)])
    assert res.exit_code != 0
    assert "cannot load artifact" in _panel_text(res.output)
    assert "not valid JSON" in _panel_text(res.output)


def test_inspect_wrong_schema_version_reports_clean_error(tmp_path):
    f = tmp_path / "v0.json"
    f.write_text('{"schema_version": 0, "trace": {}}', encoding="utf-8")
    res = runner.invoke(app, ["inspect", str(f)])
    assert res.exit_code != 0
    assert "schema_version" in _panel_text(res.output)  # version-mismatch message survives


def test_inspect_schema_mismatch_reports_clean_error(tmp_path):
    f = tmp_path / "wrong.json"
    f.write_text('{"schema_version": 1, "trace": {"nope": 1}}', encoding="utf-8")
    res = runner.invoke(app, ["inspect", str(f)])
    assert res.exit_code != 0
    assert "does not match the artifact schema" in _panel_text(res.output)


def test_explain_missing_file_reports_clean_error(tmp_path):
    # explain loads through a store, a different code path than inspect/diff — guard it too.
    res = runner.invoke(app, ["explain", str(tmp_path / "nope.json"), "anything"])
    assert res.exit_code != 0
    assert "cannot load artifact" in _panel_text(res.output)
    assert "file not found" in _panel_text(res.output)


def test_query_skips_stray_non_artifact_json_in_directory(tmp_path):
    # A directory of artifacts may also hold unrelated JSON. Strays must be skipped (with a
    # warning), not crash the whole query — but the real artifact must still match. Covers BOTH
    # a top-level object (package.json) AND a top-level array (export.json, a data export) — the
    # latter used to crash _load_many with an uncaught AttributeError (review finding #1).
    d = tmp_path / "arts"
    d.mkdir()
    _write_linear_trace(
        d / "good.json",
        "G",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("call_tool", StepKind.TOOL, StepStatus.ERROR),
    )
    (d / "package.json").write_text('{"name": "not-a-trace"}', encoding="utf-8")
    (d / "export.json").write_text('[{"row": 1}, {"row": 2}]', encoding="utf-8")
    res = runner.invoke(app, ["query", "tool-failure", str(d)])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output  # the real artifact still matched
    warn = _panel_text(res.output)
    assert "skipped 2 non-artifact" in warn  # both strays skipped, not silently/crashing
    assert "package.json" in warn and "export.json" in warn


def test_query_directory_of_only_strays_errors_cleanly(tmp_path):
    d = tmp_path / "arts"
    d.mkdir()
    (d / "a.json").write_text("{ not json", encoding="utf-8")
    (d / "b.json").write_text('{"schema_version": 99}', encoding="utf-8")
    res = runner.invoke(app, ["query", "error", str(d)])
    assert res.exit_code != 0
    assert "no valid artifacts found" in _panel_text(res.output)


def test_query_explicit_bad_file_is_fatal_not_skipped(tmp_path):
    # A file named directly on the CLI is strict: a load failure is fatal, NOT silently
    # skipped the way a stray discovered inside a directory would be.
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = runner.invoke(app, ["query", "error", str(bad)])
    assert res.exit_code != 0
    assert "cannot load artifact" in _panel_text(res.output)
    assert "not valid JSON" in _panel_text(res.output)


def test_query_skips_deeply_nested_stray_in_directory(tmp_path):
    # A deeply-nested stray .json overflows json's recursive scanner with a RecursionError
    # (not OSError/ValueError) — it used to escape _load_many and crash the query with a raw
    # traceback. It must now be skipped like any other non-artifact (review finding #8).
    d = tmp_path / "arts"
    d.mkdir()
    _write_linear_trace(
        d / "good.json",
        "G",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("call_tool", StepKind.TOOL, StepStatus.ERROR),
    )
    depth = 100_000
    (d / "deep.json").write_text("[" * depth + "]" * depth, encoding="utf-8")
    res = runner.invoke(app, ["query", "tool-failure", str(d)])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output  # the real artifact still matched
    assert "deep.json" in _panel_text(res.output)  # the stray was warned, not crashed


def test_query_flushes_skip_warning_before_fatal_explicit_error(tmp_path):
    # A stray discovered in a directory must not be silently dropped when a *later* explicit
    # file fails fatally — the skip warning is flushed before the fatal error aborts the load
    # (review finding #6).
    d = tmp_path / "arts"
    d.mkdir()
    (d / "stray.json").write_text('{"name": "x"}', encoding="utf-8")
    bad = tmp_path / "explicit_bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = runner.invoke(app, ["query", "error", str(d), str(bad)])
    assert res.exit_code != 0
    out = _panel_text(res.output)
    assert "stray.json" in out  # the earlier-discovered stray was reported, not dropped
    assert "cannot load artifact" in out  # and the explicit file's fatal error is shown


def test_artifact_loads_normalizes_deep_nesting_to_valueerror():
    # The RecursionError from json's recursive scanner is normalized to ValueError so the load
    # boundary (CLI's (OSError, ValueError) handler) treats it as bad input, not a crash.
    deep = "[" * 100_000 + "]" * 100_000
    with pytest.raises(ValueError):
        artifact.loads(deep)


def test_artifact_loads_missing_trace_key_is_valueerror():
    # A dict with the right schema_version but no "trace" key used to raise a bare KeyError
    # (neither OSError nor ValueError) and escape the CLI load handler. It must be a ValueError.
    with pytest.raises(ValueError):
        artifact.loads('{"schema_version": 1}')


def test_query_skips_schema_only_stray_in_directory(tmp_path):
    # A `{"schema_version": 1}` stray (valid version, no "trace") must be skipped, not crash the
    # query with a raw KeyError traceback (review finding: KeyError escaped _LOAD_ERRORS).
    d = tmp_path / "arts"
    d.mkdir()
    _write_linear_trace(
        d / "good.json",
        "G",
        ("input", StepKind.CHAIN, StepStatus.OK),
        ("call_tool", StepKind.TOOL, StepStatus.ERROR),
    )
    (d / "headeronly.json").write_text('{"schema_version": 1}', encoding="utf-8")
    res = runner.invoke(app, ["query", "tool-failure", str(d)])
    assert res.exit_code == 0, res.output
    assert "call_tool" in res.output  # the real artifact still matched
    assert "headeronly.json" in _panel_text(res.output)  # the stray was warned, not crashed


def test_inspect_schema_only_file_reports_clean_error(tmp_path):
    # The same input named explicitly is a clean fatal error, not a raw KeyError traceback.
    f = tmp_path / "headeronly.json"
    f.write_text('{"schema_version": 1}', encoding="utf-8")
    res = runner.invoke(app, ["inspect", str(f)])
    assert res.exit_code != 0
    assert "cannot load artifact" in _panel_text(res.output)
    assert "missing the required" in _panel_text(res.output)


def test_explain_exact_id_wins_over_suffix_collision(tmp_path):
    # "c" is a full step id AND a suffix of "ns:c". Passing the full id must resolve to that
    # exact step, not be rejected as ambiguous (the Tier-2 explain bug).
    p = tmp_path / "ns.json"
    steps = [
        Step(step_id="c", trace_id="N", seq=0, name="root_step", kind=StepKind.CHAIN),
        Step(step_id="ns:c", trace_id="N", seq=1, name="child_step", kind=StepKind.CHAIN),
    ]
    edges = [Edge(type=EdgeType.CAUSED_BY, src="ns:c", dst="c")]
    nt = normalize(
        RawTrace(trace=Trace(trace_id="N", source_kind="x"), steps=steps, causal_edges=edges)
    )
    artifact.save(nt, p)

    res = runner.invoke(app, ["explain", str(p), "c"])
    assert res.exit_code == 0, res.output
    assert "root_step" in res.output   # explained the exact "c" step ...
    assert "no causes" in res.output    # ... which is a root, so it has no ancestors

    # The namespaced id still resolves exactly to its own step.
    res2 = runner.invoke(app, ["explain", str(p), "ns:c"])
    assert res2.exit_code == 0, res2.output
    assert "child_step" in res2.output
    assert "← root_step" in res2.output


def test_explain_ambiguous_suffix_still_rejected(tmp_path):
    # Two ids share the "abc" suffix and neither equals it -> genuinely ambiguous; still an error.
    p = tmp_path / "amb.json"
    steps = [
        Step(step_id="x1abc", trace_id="N", seq=0, name="a", kind=StepKind.CHAIN),
        Step(step_id="x2abc", trace_id="N", seq=1, name="b", kind=StepKind.CHAIN),
    ]
    edges = [Edge(type=EdgeType.CAUSED_BY, src="x2abc", dst="x1abc")]
    nt = normalize(
        RawTrace(trace=Trace(trace_id="N", source_kind="x"), steps=steps, causal_edges=edges)
    )
    artifact.save(nt, p)
    res = runner.invoke(app, ["explain", str(p), "abc"])
    assert res.exit_code != 0
    assert "matched 2 steps" in _panel_text(res.output)


def test_load_reason_maps_each_failure_to_a_short_phrase():
    # Lock the exception -> single-line reason mapping directly (no Rich panel in the way).
    # pytest.raises guarantees the exception actually fires, so the asserts can't no-op.
    import json as _json

    from pydantic import ValidationError

    from tracegraph.model import NormalizedTrace

    assert cli_mod._load_reason(FileNotFoundError()) == "file not found"
    assert cli_mod._load_reason(IsADirectoryError()) == "is a directory, not a file"
    assert cli_mod._load_reason(PermissionError()) == "permission denied"

    with pytest.raises(_json.JSONDecodeError) as ei_json:
        _json.loads("{bad")
    assert cli_mod._load_reason(ei_json.value) == "not valid JSON"

    with pytest.raises(ValidationError) as ei_val:
        NormalizedTrace.model_validate({"nope": 1})
    assert cli_mod._load_reason(ei_val.value) == "does not match the artifact schema"

    # A non-object artifact (e.g. a top-level array) is a ValueError raised by artifact.loads;
    # its message passes through unchanged (review finding #1's hardening path).
    with pytest.raises(ValueError) as ei_arr:
        artifact.loads("[1, 2, 3]")
    assert "must be a top-level object" in cli_mod._load_reason(ei_arr.value)

    # A plain ValueError (e.g. the schema_version mismatch) passes its first line through.
    assert (
        cli_mod._load_reason(ValueError("unsupported artifact schema_version 0\ndetail"))
        == "unsupported artifact schema_version 0"
    )


def test_artifact_size_limit_env_var(tmp_path, monkeypatch):
    # The per-file byte ceiling is configurable: a tiny limit rejects the file with a
    # clean error naming the env var; 0 disables the check entirely.
    p = tmp_path / "a.json"
    _write_linear_trace(p, "SZ", ("plan", StepKind.CHAIN, StepStatus.OK))
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "10")
    res = runner.invoke(app, ["query", "error", str(p)])
    assert res.exit_code != 0
    flat = res.output.replace("\n", "")
    assert "byte limit" in flat and "TRACEGRAPH_MAX_ARTIFACT_BYTES" in flat

    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "0")
    res = runner.invoke(app, ["query", "error", str(p)])
    assert res.exit_code == 1  # loads fine; this clean trace simply has no matches
    assert "no matches" in res.output


def test_artifact_size_limit_covers_non_query_commands(tmp_path, monkeypatch):
    # The byte ceiling guards every file-load boundary, not just query/export batches.
    p = tmp_path / "a.json"
    _write_linear_trace(p, "SZ2", ("plan", StepKind.CHAIN, StepStatus.OK))
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "10")
    for args in (["validate", str(p)], ["inspect", str(p)], ["analyze", str(p)],
                 ["explain", str(p), "SZ20"]):
        res = runner.invoke(app, args)
        assert res.exit_code != 0, args
        assert "byte limit" in res.output.replace("\n", ""), args


def test_ingest_otlp_respects_size_limit(tmp_path, monkeypatch):
    # ingest-otlp reads through the same bounded loader as every other file boundary.
    p = tmp_path / "spans.json"
    p.write_text('{"resourceSpans": []}', encoding="utf-8")
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "5")
    res = runner.invoke(app, ["ingest-otlp", "--file", str(p)])
    assert res.exit_code != 0
    assert "byte limit" in res.output.replace("\n", "")


def test_ingest_otlp_malformed_semantic_input_is_a_clean_error(tmp_path):
    # A structurally-valid JSON whose content the adapter rejects (unknown --trace id,
    # dangling parent) must surface as a clean CLI error, not a raw traceback.
    p = tmp_path / "spans.json"
    p.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t1", "spanId": "s1", "name": "root", "startTimeUnixNano": "1"},
    ]}]}]}), encoding="utf-8")
    res = runner.invoke(app, ["ingest-otlp", "--file", str(p), "--trace", "missing"])
    assert res.exit_code != 0
    assert "cannot ingest OTLP export" in res.output.replace("\n", "")

    p.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [
        {"traceId": "t1", "spanId": "s1", "name": "child",
         "parentSpanId": "GONE", "startTimeUnixNano": "1"},
    ]}]}]}), encoding="utf-8")
    res = runner.invoke(app, ["ingest-otlp", "--file", str(p)])
    assert res.exit_code != 0
    assert "cannot ingest OTLP export" in res.output.replace("\n", "")


def test_ingest_otlp_malformed_nested_shapes_are_clean_errors(tmp_path):
    # Non-object entries inside resourceSpans/scopeSpans/spans must be a clean CLI
    # error, not an AttributeError traceback.
    p = tmp_path / "bad.json"
    for payload in (
        {"resourceSpans": [1]},
        {"resourceSpans": [{"scopeSpans": [1]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [1]}]}]},
    ):
        p.write_text(json.dumps(payload), encoding="utf-8")
        res = runner.invoke(app, ["ingest-otlp", "--file", str(p)])
        assert res.exit_code != 0, payload
        assert "cannot ingest OTLP export" in res.output.replace("\n", ""), payload


def test_ingest_otlp_non_array_containers_are_clean_errors(tmp_path):
    # Truthy AND falsey non-array container values at every level must be clean errors —
    # an `or []` would silently read `{}`/`0` as "no spans".
    p = tmp_path / "bad2.json"
    for payload in (
        {"resourceSpans": 1},
        {"resourceSpans": {}},
        {"resourceSpans": [{"scopeSpans": 0}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": {}}]}]},
    ):
        p.write_text(json.dumps(payload), encoding="utf-8")
        res = runner.invoke(app, ["ingest-otlp", "--file", str(p)])
        assert res.exit_code != 0, payload
        assert "cannot ingest OTLP export" in res.output.replace("\n", ""), payload


def test_ingest_otlp_malformed_links_are_clean_errors(tmp_path):
    # `links` gets the same strict array validation as the span containers.
    p = tmp_path / "bad3.json"
    span = {"traceId": "t1", "spanId": "s1", "name": "root", "startTimeUnixNano": "1"}
    for links in ({}, 1, [1]):
        span["links"] = links
        p.write_text(json.dumps(
            {"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}
        ), encoding="utf-8")
        res = runner.invoke(app, ["ingest-otlp", "--file", str(p)])
        assert res.exit_code != 0, links
        assert "cannot ingest OTLP export" in res.output.replace("\n", ""), links


def test_size_limit_config_errors_are_not_per_file_invalids(tmp_path, monkeypatch):
    # A bad TRACEGRAPH_MAX_ARTIFACT_BYTES is a configuration error: validate must abort
    # cleanly, never report valid files as INVALID; a value beyond the documented sanity
    # bound is rejected the same way.
    p = tmp_path / "a.json"
    _write_linear_trace(p, "CFG", ("plan", StepKind.CHAIN, StepStatus.OK))

    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "-1")
    res = runner.invoke(app, ["validate", str(p)])
    assert res.exit_code != 0
    assert "INVALID" not in res.output
    assert "must be >= 0" in res.output.replace("\n", "")

    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(2**63))
    res = runner.invoke(app, ["query", "error", str(p)])
    assert res.exit_code != 0
    assert "too large" in res.output.replace("\n", "")


def test_size_limit_accepted_boundary_reads_files_fine(tmp_path, monkeypatch):
    # The maximum accepted limit must still READ normal files — chunked reads never
    # pre-allocate the configured limit (read(limit+1) would MemoryError here).
    p = tmp_path / "a.json"
    _write_linear_trace(p, "BND", ("plan", StepKind.CHAIN, StepStatus.OK))
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(2**63 - 2))
    res = runner.invoke(app, ["validate", str(p)])
    assert res.exit_code == 0, res.output
    assert "1 artifact(s) valid" in res.output


def test_bounded_reader_requests_at_most_remaining_allowance(tmp_path, monkeypatch):
    # With a limit set, every read() must request exactly the REMAINING allowance
    # (+1 rejection byte), even across short reads — a tiny limit must not allocate a
    # full 8MiB chunk, and a short read must shrink the next request, never repeat it.
    # The spy returns at most 3 bytes per call to force the partial-read path.
    p = tmp_path / "big.json"
    p.write_bytes(b"x" * 1024)
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", "10")
    requested: list[int] = []
    real_open = builtins.open

    class _SpyFile:
        def __init__(self, handle):
            self._h = handle

        def read(self, n=-1):
            requested.append(n)
            return self._h.read(min(n, 3) if n >= 0 else 3)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._h.close()

    def spy_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return _SpyFile(handle) if "b" in mode and str(file) == str(p) else handle

    monkeypatch.setattr(cli_mod, "open", spy_open, raising=False)
    with pytest.raises(ValueError, match="byte limit"):
        cli_mod._read_artifact_bytes(p)
    # 3-byte short reads against a 10-byte limit: allowance shrinks 11 -> 8 -> 5 -> 2,
    # then the 2-byte read tips the total to 11 and the limit rejects.
    assert requested == [11, 8, 5, 2], requested
