"""CLI smoke test: ingest a real SqliteSaver DB, then inspect / explain / diff."""

import builtins
import errno
import json
import os
import re
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import textwrap
import threading
import time

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
    StepSource,
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


def _paused_artifact(tmp_path):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from tracegraph.adapters import LangGraphCheckpointAdapter

    def ask(state):
        interrupt("approve?")
        return {"x": 1}

    graph = StateGraph(dict)
    graph.add_node("plan", lambda state: {"x": 0})
    graph.add_node("approve", ask)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "approve")
    graph.add_edge("approve", END)
    with SqliteSaver.from_conn_string(":memory:") as saver:
        graph.compile(checkpointer=saver).invoke({"x": 0}, {"configurable": {"thread_id": "t"}})
        nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    path = tmp_path / "paused.json"
    artifact.save(nt, path)
    return nt, path


def _inspect_at_width(monkeypatch, path, width):
    """Rich wraps at the console width, which otherwise follows the caller's terminal."""
    from rich.console import Console

    monkeypatch.setattr(cli_mod, "console", Console(width=width))
    res = runner.invoke(app, ["inspect", str(path)])
    assert res.exit_code == 0, res.output
    return res.output


def test_inspect_marks_an_interrupted_step_and_counts_it(tmp_path, monkeypatch):
    _, path = _paused_artifact(tmp_path)
    out = _inspect_at_width(monkeypatch, path, 200)
    assert "2: approve  ⏸ interrupted\n" in out
    assert out.rstrip().endswith("4 steps · 0 tool · 0 error · 0 lossy-projection · 1 interrupted")

    # Narrow terminals wrap and indent; the content must survive that.
    narrow = " ".join(_inspect_at_width(monkeypatch, path, 40).split())
    assert "2: approve ⏸ interrupted" in narrow
    assert "0 lossy-projection · 1 interrupted" in narrow


def test_interrupted_label_is_not_styled_as_success(tmp_path):
    nt, _ = _paused_artifact(tmp_path)
    [step] = [s for s in nt.steps if s.source is StepSource.INTERRUPT]
    assert cli_mod._label(step) == "[yellow]2: approve  ⏸ interrupted[/]"


def test_interrupted_label_escapes_the_name_and_keeps_the_lossy_suffix():
    step = Step(
        step_id="s",
        trace_id="t",
        seq=3,
        name="[bold]plan",
        source=StepSource.INTERRUPT,
        status=StepStatus.UNSET,
        projection_lossy=True,
    )
    # "[bold]" is a real Rich tag, so an unescaped name would be restyled and lose its text.
    assert cli_mod._label(step) == r"[yellow]3: \[bold]plan  ⏸ interrupted[/]  ⚠ lossy-projection"


def test_inspect_counts_every_interrupted_task(tmp_path, monkeypatch):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from tracegraph.adapters import LangGraphCheckpointAdapter

    def asks(name):
        def body(state):
            interrupt(name)
            return {}

        return body

    graph = StateGraph(dict)
    graph.add_node("a", lambda state: {"x": 0})
    graph.add_node("x", asks("x"))
    graph.add_node("y", asks("y"))
    graph.add_edge(START, "a")
    graph.add_edge("a", "x")
    graph.add_edge("a", "y")
    graph.add_edge("x", END)
    graph.add_edge("y", END)
    with SqliteSaver.from_conn_string(":memory:") as saver:
        graph.compile(checkpointer=saver).invoke({"x": 0}, {"configurable": {"thread_id": "t"}})
        nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    path = tmp_path / "two.json"
    artifact.save(nt, path)
    out = _inspect_at_width(monkeypatch, path, 200)
    assert out.count("⏸ interrupted") == 2
    assert out.rstrip().endswith("· 2 interrupted")


def test_inspect_summary_omits_interrupts_when_there_are_none(artifacts, monkeypatch):
    a_json, _ = artifacts
    out = _inspect_at_width(monkeypatch, a_json, 200)
    assert "interrupted" not in out
    assert out.rstrip().endswith("lossy-projection")


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


def _patch_px(monkeypatch, fake):
    """Route every ``px`` call to ``fake(command, timeout=...)``.

    The fake returns an object whose ``stdout`` is the text px would print, or raises what the
    subprocess would (``FileNotFoundError``, ``TimeoutExpired``, ``CalledProcessError``).
    """
    monkeypatch.setattr(
        cli_mod, "_px_exec",
        lambda command, *, timeout: fake(command, timeout=timeout).stdout.encode("utf-8"),
    )


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

    _patch_px(monkeypatch, fake_run)
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
    assert seen[1][1] == {"timeout": cli_mod._PX_TIMEOUT_SECONDS}
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

    _patch_px(monkeypatch, missing)
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

    _patch_px(monkeypatch, fake_run)
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

    _patch_px(monkeypatch, fake_run)
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

    _patch_px(monkeypatch, fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose"])
    assert result.exit_code == 0, result.output
    assert "No failed trace found in the latest 2" in result.output
    assert "newest" in result.output


def test_phoenix_diagnose_empty_project_is_actionable(monkeypatch):
    def fake_run(command, **kwargs):
        return SimpleNamespace(stdout="1.8.1" if command == ["px", "--version"] else "[]")

    _patch_px(monkeypatch, fake_run)
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

    _patch_px(monkeypatch, fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose"])
    assert result.exit_code != 0
    assert "required" in result.output and "status" in result.output


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (subprocess.TimeoutExpired(["px"], 60), "timed out"),
        (subprocess.CalledProcessError(1, ["px"], stderr="password=secret"), "failed with exit 1"),
        (PermissionError(errno.EACCES, "password=secret"), "check that `px` on PATH is an executable file"),
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

    _patch_px(monkeypatch, fake_run)
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code == 2
    assert expected in _flat(result.output)
    assert "password=secret" not in result.output


def test_phoenix_diagnose_rejects_malformed_px_json(monkeypatch):
    responses = iter(["1.8.1", "not-json"])
    _patch_px(
        monkeypatch,
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code != 0
    assert "returned invalid JSON" in result.output


@pytest.mark.parametrize("version", ["0.9.9", "unknown"])
def test_phoenix_diagnose_rejects_unsupported_px_versions(monkeypatch, version):
    _patch_px(
        monkeypatch,
        lambda *args, **kwargs: SimpleNamespace(stdout=version),
    )
    result = runner.invoke(app, ["phoenix", "diagnose", "trace"])
    assert result.exit_code != 0
    assert "1.0.4" in result.output


def test_phoenix_doctor_reports_ready_warn_and_fail(monkeypatch):
    responses = iter(["1.8.1", "[]", json.dumps([_phoenix_export(status="OK")])])
    _patch_px(
        monkeypatch,
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    ready = runner.invoke(app, ["phoenix", "doctor", "--project", "agent-prod"])
    assert ready.exit_code == 0, ready.output
    assert "READY" in ready.output and "agent-prod" in ready.output

    responses = iter(["1.8.1", "[]", "[]"])
    _patch_px(
        monkeypatch,
        lambda *args, **kwargs: SimpleNamespace(stdout=next(responses)),
    )
    warning = runner.invoke(app, ["phoenix", "doctor"])
    assert warning.exit_code == 0, warning.output
    assert "WARN" in warning.output and "no traces yet" in warning.output

    _patch_px(
        monkeypatch,
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


def _broken_px(bin_dir, kind):
    if kind == "directory":
        (bin_dir / "px").mkdir()
        return
    px = bin_dir / "px"
    if kind == "not executable":
        px.write_text("#!/bin/sh\necho 1.8.1\n")
        px.chmod(0o644)
    else:  # an executable whose header no loader accepts
        px.write_bytes(b"\x7fELF\x00\x00 not a real executable")
        px.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec semantics")
@pytest.mark.parametrize("kind", ["not executable", "directory", "bad format"])
def test_phoenix_commands_report_a_px_that_cannot_be_run(tmp_path, monkeypatch, kind):
    if kind == "not executable" and os.geteuid() == 0:
        pytest.skip("root may execute a file without the exec bit")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _broken_px(bin_dir, kind)
    # The fixture alone: on EACCES or ENOEXEC the PATH search moves on to any later px.
    monkeypatch.setenv("PATH", str(bin_dir))
    doctor = runner.invoke(app, ["phoenix", "doctor"])
    assert doctor.exit_code == 1, doctor.output
    stderr = _flat(doctor.stderr)
    assert stderr.startswith("FAIL Phoenix CLI `px` failed with an OS error (") and "Traceback" not in doctor.output
    assert stderr.endswith("); check that `px` on PATH is an executable file")
    for args in (["phoenix", "diagnose"], ["phoenix", "diagnose", "trace"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert "check that `px` on PATH is an executable file" in _flat(result.output)


def _px_script(tmp_path, body):
    """A fake ``px`` run as ``[sys.executable, script]``: portable, no PATH or shebang."""
    script = tmp_path / "fake_px.py"
    script.write_text("import os, sys, time\n" + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def _px_limit(monkeypatch, limit):
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(limit))


def _reaped(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("size", [64, 65])
def test_px_output_at_and_past_the_byte_limit(tmp_path, monkeypatch, size):
    _px_limit(monkeypatch, 64)
    command = _px_script(tmp_path, f"sys.stdout.write('x' * {size})\n")
    if size == 64:
        assert cli_mod._px_run(command, action="px probe") == "x" * 64
    else:
        with pytest.raises(cli_mod._PxError, match=r"^px probe output exceeded the 64-byte limit"):
            cli_mod._px_run(command, action="px probe")


@pytest.mark.skipif(os.name == "nt", reason="pid liveness check is POSIX")
def test_px_endless_output_is_stopped_and_reaped(tmp_path, monkeypatch):
    _px_limit(monkeypatch, 1 << 20)
    pid_file = tmp_path / "pid"
    command = _px_script(tmp_path, f"""
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        while True:
            sys.stdout.write("y" * 65536)
    """)
    started = time.monotonic()
    with pytest.raises(cli_mod._PxError, match="exceeded the 1048576-byte limit"):
        cli_mod._px_run(command, action="px probe", timeout=30)
    assert time.monotonic() - started < 10
    assert _reaped(int(pid_file.read_text()))


def test_px_short_writes_and_split_utf8_are_assembled(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_READ_CHUNK_BYTES", 3)
    text = "a€b" * 50 + "é"
    command = _px_script(tmp_path, f"""
        for ch in {text!r}:
            sys.stdout.write(ch)
            sys.stdout.flush()
    """)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    assert cli_mod._px_run(command, action="px probe") == text


def test_px_output_that_is_not_utf8_is_refused(tmp_path):
    command = _px_script(tmp_path, "sys.stdout.buffer.write(b'{\"x\": \"\\xff\"}')\n")
    with pytest.raises(cli_mod._PxError, match=r"^px probe returned output that is not UTF-8 text$"):
        cli_mod._px_run(command, action="px probe")


def test_px_stderr_is_discarded_whatever_its_size(tmp_path, monkeypatch):
    _px_limit(monkeypatch, 16)
    command = _px_script(tmp_path, """
        sys.stderr.write("password=secret " * (20 * 1024 * 1024 // 16))
        sys.stdout.write("ok")
    """)
    assert cli_mod._px_run(command, action="px probe") == "ok"


def test_px_limit_zero_disables_the_ceiling(tmp_path, monkeypatch):
    _px_limit(monkeypatch, 0)
    command = _px_script(tmp_path, "sys.stdout.write('z' * 300000)\n")
    assert len(cli_mod._px_run(command, action="px probe")) == 300000


def test_px_nonzero_exit_reports_the_actual_code(tmp_path):
    command = _px_script(tmp_path, "sys.stdout.write('partial'); sys.exit(7)\n")
    with pytest.raises(cli_mod._PxError, match=r"^px probe failed with exit 7; verify Phoenix endpoint"):
        cli_mod._px_run(command, action="px probe")


@pytest.mark.skipif(os.name == "nt", reason="pid liveness check is POSIX")
def test_px_timeout_kills_and_reaps_the_child(tmp_path):
    pid_file = tmp_path / "pid"
    command = _px_script(tmp_path, f"""
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
    """)
    with pytest.raises(cli_mod._PxError, match=r"^px probe timed out after 1 seconds$"):
        cli_mod._px_run(command, action="px probe", timeout=1)
    assert _reaped(int(pid_file.read_text()))


@pytest.mark.skipif(os.name == "nt", reason="pid liveness check is POSIX")
def test_px_descendant_holding_stdout_does_not_hold_the_call(tmp_path):
    # With a pipe, a descendant that inherits stdout keeps it open after px exits.
    pid_file = tmp_path / "grandchild"
    command = _px_script(tmp_path, f"""
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        sys.stdout.write("done")
    """)
    try:
        started = time.monotonic()
        assert cli_mod._px_run(command, action="px probe", timeout=20) == "done"
        assert time.monotonic() - started < 10
    finally:
        os.kill(int(pid_file.read_text()), 9)


def test_px_output_written_after_the_exit_size_is_not_captured(tmp_path, monkeypatch):
    # A process px leaves behind writes after the size was sampled; the capture stops there.
    go, done = tmp_path / "go", tmp_path / "done"
    writer = (
        "import os, sys, time\n"
        f"deadline = time.monotonic() + 30\n"
        f"while not os.path.exists({str(go)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('BBBB'); sys.stdout.flush()\n"
        f"open({str(done)!r}, 'w').close()\n"
    )
    command = _px_script(tmp_path, f"""
        import subprocess
        sys.stdout.write("AAAA"); sys.stdout.flush()
        subprocess.Popen([sys.executable, "-c", {writer!r}])
    """)
    real = cli_mod._read_px_output

    def at_the_boundary(out, size):
        go.touch()
        deadline = time.monotonic() + 10
        while not done.exists():
            assert time.monotonic() < deadline, "the late writer never wrote"
            time.sleep(0.01)
        return real(out, size)

    monkeypatch.setattr(cli_mod, "_read_px_output", at_the_boundary)
    assert cli_mod._px_run(command, action="px probe") == "AAAA"


@pytest.mark.skipif(not hasattr(os, "pread"), reason="positional reads are POSIX")
def test_px_capture_read_does_not_rewind_the_shared_offset():
    # Every process px left behind writes through the same file offset. A reader that rewound it
    # would let a write landing between its seek and its read overwrite the capture.
    class SharedOffset:
        def __init__(self, f):
            self._f = f

        def fileno(self):
            return self._f.fileno()

        def seek(self, *args):
            position = self._f.seek(*args)
            os.write(self._f.fileno(), b"BBBB")  # the descendant, right after the rewind
            return position

        def read(self, size=-1):
            return self._f.read(size)

    with tempfile.TemporaryFile() as f:
        f.write(b"AAAA")
        f.flush()
        assert cli_mod._read_px_output(SharedOffset(f), 4) == b"AAAA"


def test_px_overflow_wins_over_a_passed_deadline(tmp_path, monkeypatch):
    # Both are true at the same poll; the order is fixed, not a race.
    _px_limit(monkeypatch, 4)
    killed = []

    class Stuck:
        def __init__(self, command, *, stdout, **kwargs):
            stdout.write(b"too long")
            stdout.flush()

        def wait(self, timeout=None):
            if killed:
                return -9
            raise subprocess.TimeoutExpired("px", timeout)

        def poll(self):
            return -9 if killed else None

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(cli_mod.subprocess, "Popen", Stuck)
    with pytest.raises(cli_mod._PxOutputTooLarge):
        cli_mod._px_exec(["px"], timeout=0)
    assert killed == [True]


@pytest.mark.parametrize("exit_code", [0, 3])
def test_px_exit_observed_after_the_deadline_is_a_timeout(tmp_path, monkeypatch, exit_code):
    clock = iter([0.0, 5.0, 5.0, 5.0])  # deadline set at 0; the exit is seen at 5 s

    class Late:
        def __init__(self, command, **kwargs):
            pass

        def wait(self, timeout=None):
            return exit_code

        def poll(self):
            return exit_code

    monkeypatch.setattr(cli_mod.subprocess, "Popen", Late)
    monkeypatch.setattr(cli_mod.time, "monotonic", lambda: next(clock))
    with pytest.raises(subprocess.TimeoutExpired):
        cli_mod._px_exec(["px"], timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="pid liveness check is POSIX")
def test_px_interrupt_kills_and_reaps_the_child(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    command = _px_script(tmp_path, f"""
        # Published by rename, so the file never exists half-written.
        open({str(pid_file)!r} + ".tmp", "w").write(str(os.getpid()))
        os.replace({str(pid_file)!r} + ".tmp", {str(pid_file)!r})
        time.sleep(60)
    """)
    real_wait = subprocess.Popen.wait

    def interrupted(self, timeout=None):
        if timeout is not None and pid_file.exists():
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli_mod._px_run(command, action="px probe", timeout=30)
    assert _reaped(int(pid_file.read_text()))


def test_px_repeated_timeouts_leave_no_threads_or_descriptors(tmp_path):
    command = _px_script(tmp_path, "time.sleep(60)\n")
    fd_dir = Path("/dev/fd")
    before = (threading.active_count(), len(os.listdir(fd_dir)) if fd_dir.is_dir() else 0)
    for _ in range(3):
        with pytest.raises(cli_mod._PxError, match="timed out"):
            cli_mod._px_run(command, action="px probe", timeout=0.2)
    after = (threading.active_count(), len(os.listdir(fd_dir)) if fd_dir.is_dir() else 0)
    assert after == before


@pytest.mark.parametrize("command", [["phoenix", "doctor"], ["phoenix", "diagnose", "trace"]])
def test_phoenix_commands_report_oversized_px_output(tmp_path, monkeypatch, command):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    px = bin_dir / "px"
    px.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('1.8.1') if sys.argv[1:] == ['--version'] else sys.stdout.write('PRIVATE ' * 64)\n",
        encoding="utf-8",
    )
    px.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    _px_limit(monkeypatch, 100)
    result = runner.invoke(app, command)
    assert result.exit_code == (1 if command[1] == "doctor" else 2), result.output
    assert "exceeded the 100-byte limit" in _flat(result.output)
    assert "PRIVATE" not in result.output


def test_px_os_errors_echo_only_the_operating_system_text(monkeypatch):
    def fake_run(command, **kwargs):
        raise OSError(errno.EMFILE, "password=secret")

    _patch_px(monkeypatch, fake_run)
    result = runner.invoke(app, ["phoenix", "doctor"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    expected = f"FAIL Phoenix CLI `px` failed with an OS error ({os.strerror(errno.EMFILE)})"
    assert _flat(result.stderr) == expected
    assert "password=secret" not in result.output


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


MARKUP_NAME = "tool[/]INJECT[bold red]"


def _markup_trace(path):
    """An artifact whose step names and error text contain Rich markup metacharacters.

    Span names come from producer telemetry, so brackets are ordinary content: "tool[0]",
    "[Errno 2] No such file". Rich parses them as markup tags — unescaped, that either
    raises MarkupError (killing the command) or silently deletes the bracketed text from
    what the operator reads.
    """
    steps = [
        Step(step_id="m0", trace_id="M", seq=0, name=MARKUP_NAME, kind=StepKind.CHAIN),
        Step(
            step_id="m1",
            trace_id="M",
            seq=1,
            name=MARKUP_NAME,
            kind=StepKind.TOOL,
            status=StepStatus.ERROR,
            error_msg="boom [bold red]detail[/]",
        ),
    ]
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id="M", source_kind="x"),
            steps=steps,
            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="m1", dst="m0")],
        )
    )
    artifact.save(nt, path)
    return path


def test_inspect_does_not_swallow_markup_in_names_or_errors(tmp_path):
    path = _markup_trace(tmp_path / "M.json")
    res = runner.invoke(app, ["inspect", str(path)])
    assert res.exit_code == 0, res.output
    assert MARKUP_NAME in res.output, res.output
    assert "boom [bold red]detail[/]" in res.output, res.output


def test_explain_does_not_crash_on_markup_in_names(tmp_path):
    path = _markup_trace(tmp_path / "M.json")
    res = runner.invoke(app, ["explain", str(path), "m1"])
    assert res.exit_code == 0, res.output
    assert MARKUP_NAME in res.output, res.output


def test_query_does_not_crash_on_markup_in_labels(tmp_path):
    path = _markup_trace(tmp_path / "M.json")
    res = runner.invoke(app, ["query", "tool-failure", str(path)])
    assert res.exit_code == 0, res.output
    assert MARKUP_NAME in res.output, res.output


@pytest.mark.parametrize(
    "command", [["inspect"], ["explain", "{path}", "e1"], ["query", "tool-failure"], ["analyze"]]
)
def test_cli_prints_an_emoji_code_in_producer_strings_literally(tmp_path, command):
    """Escaping markup does not stop Rich's emoji substitution: ":ok:" would print as an emoji
    the artifact and the JSON reports do not hold."""
    steps = [
        Step(step_id="e0", trace_id="E", seq=0, name=":ok:", kind=StepKind.CHAIN),
        Step(step_id="e1", trace_id="E", seq=1, name=":ok:", kind=StepKind.TOOL,
             status=StepStatus.ERROR, error_msg="boom :fire:"),
    ]
    nt = normalize(RawTrace(trace=Trace(trace_id="E", source_kind="x"), steps=steps,
                            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="e1", dst="e0")]))
    path = tmp_path / "E.json"
    artifact.save(nt, path)
    args = [a.format(path=path) for a in command] if "{path}" in command else [*command, str(path)]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert ":ok:" in res.output, res.output
    assert "\N{SQUARED OK}" not in res.output and "\N{FIRE}" not in res.output, res.output


def _run_main(*args, cwd):
    """Run the real entry point: CliRunner(app) bypasses ``main()`` and its stream setup."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # strict errors, as on a UTF-8 terminal
    return subprocess.run(
        [sys.executable, "-c", "from tracegraph.cli import main; main()", *map(str, args)],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )


def _surrogate_trace(path):
    """JSON can carry a lone surrogate ("\\ud800"); the artifact loads and validates."""
    steps = [
        Step(step_id="s0", trace_id="S", seq=0, kind=StepKind.CHAIN),
        Step(step_id="s1", trace_id="S", seq=1, name="\ud800", kind=StepKind.TOOL,
             status=StepStatus.ERROR, error_msg="boom \ud800"),
    ]
    nt = normalize(RawTrace(trace=Trace(trace_id="S", source_kind="x", run_id="run-\ud800"),
                            steps=steps,
                            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="s1", dst="s0")]))
    artifact.save(nt, path)
    return path


@pytest.mark.parametrize(
    "command",
    [["analyze"], ["inspect"], ["explain", "{path}", "s1"], ["query", "tool-failure"],
     ["summarize", "tool-failure"], ["export-incident-memo", "{path}", "--out", "memo.md"]],
)
def test_cli_survives_a_lone_surrogate_in_producer_strings(tmp_path, command):
    path = _surrogate_trace(tmp_path / "S.json")
    args = [a.format(path=path) for a in command] if "{path}" in command else [*command, path]
    res = _run_main(*args, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "Traceback" not in res.stderr, res.stderr


def test_main_runs_with_an_unused_output_stream_closed(tmp_path):
    code = "import sys; sys.stderr.close(); from tracegraph.cli import main; main()"
    res = subprocess.run([sys.executable, "-c", code, "--version"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip(), res.stdout


def test_ingest_otlp_default_path_survives_a_lone_surrogate_trace_id(tmp_path):
    (tmp_path / "otlp.json").write_text(json.dumps(_otlp_export("\ud800")), encoding="utf-8")
    res = _run_main("ingest-otlp", "--file", "otlp.json", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    [written] = tmp_path.glob("trace-*.json")
    assert artifact.load(written).trace.trace_id == "\ud800"


def test_analyze_reports_an_unwritable_json_out_as_a_usage_error(tmp_path):
    """The analysis has already run and rendered by then; a traceback would discard it."""
    path = _markup_trace(tmp_path / "M.json")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        res = runner.invoke(app, ["analyze", str(path), "--json-out", str(locked / "r.json")])
        assert res.exit_code == 2, res.output
        assert "cannot write report" in _panel_text(res.output)
        assert res.exception is None or isinstance(res.exception, SystemExit)
    finally:
        locked.chmod(0o755)


def test_ingest_refuses_to_overwrite_an_existing_default_output(artifacts, tmp_path, monkeypatch):
    """README guarantees this; nothing covered it."""
    a_json, _ = artifacts
    db = a_json.parent / "trace.db"
    # A fresh cwd: the fixture already wrote A.json into tmp_path via --out.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    first = runner.invoke(app, ["ingest", "--sqlite", str(db), "--thread", "A"])
    assert first.exit_code == 0, first.output
    again = runner.invoke(app, ["ingest", "--sqlite", str(db), "--thread", "A"])
    assert again.exit_code == 2, again.output
    assert "default output exists" in _panel_text(again.output)


def test_output_may_not_alias_an_input_artifact(artifacts):
    """README guarantees outputs cannot alias an input or another output."""
    a_json, _ = artifacts
    res = runner.invoke(app, ["analyze", str(a_json), "--json-out", str(a_json)])
    assert res.exit_code == 2, res.output
    assert "must not overwrite an input" in _panel_text(res.output)


def test_inspect_header_survives_markup_in_trace_id_and_source_kind(tmp_path):
    """The tree header is producer-controlled too, not just the step labels."""
    nt = normalize(
        RawTrace(
            trace=Trace(trace_id="t[/]x[/]", source_kind="k[bold]z"),
            steps=[Step(step_id="s0", trace_id="t[/]x[/]", seq=0, name="n")],
            causal_edges=[],
        )
    )
    path = tmp_path / "hdr.json"
    artifact.save(nt, path)
    res = runner.invoke(app, ["inspect", str(path)])
    assert res.exit_code == 0, res.output
    assert "t[/]x[/]" in res.output, res.output
    assert "k[bold]z" in res.output, res.output

    validated = runner.invoke(app, ["validate", str(path)])
    assert validated.exit_code == 0, validated.output
    assert "t[/]x[/]" in validated.output, validated.output


def test_version_option_prints_bare_version():
    """`--version` is what a release script parses, so it prints the number and nothing else."""
    from tracegraph import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
    assert __version__ != "0.0.0.dev0", (
        "the distribution name in tracegraph/__init__.py does not match pyproject; "
        "this fails silently, reporting a dev version for a real install"
    )


def test_version_short_flag_matches():
    assert (
        runner.invoke(app, ["-V"]).stdout.strip()
        == runner.invoke(app, ["--version"]).stdout.strip()
    )


def test_no_args_still_shows_help():
    """Adding a root callback must not turn a bare invocation into a silent success."""
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage:" in result.stdout


# --- render paths that print producer text: diff, validate, explain labels ---------------


def _named_chain(path, trace_id, *names):
    steps = [Step(step_id=f"{trace_id}{i}", trace_id=trace_id, seq=i, name=n) for i, n in enumerate(names)]
    edges = [Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}{i}", dst=f"{trace_id}{i - 1}") for i in range(1, len(names))]
    artifact.save_atomic(
        normalize(RawTrace(trace=Trace(trace_id=trace_id, source_kind="x"), steps=steps, causal_edges=edges)), path
    )
    return path


def test_diff_prints_bracketed_names_literally(tmp_path):
    a = _named_chain(tmp_path / "a.json", "A", "root", "tool[bold]", "end")
    b = _named_chain(tmp_path / "b.json", "B", "root", "x[/]", "end")
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 1
    assert "A='tool[bold]' B='x[/]'" in result.output


def test_validate_prints_bracketed_error_text_literally(tmp_path):
    path = _named_chain(tmp_path / "bad.json", "V", "a", "b")
    doc = json.loads(path.read_text())
    edge = next(e for e in doc["trace"]["edges"] if e["type"] == "CAUSED_BY")
    edge["src"] = "ghost[red]X[/red]"
    path.write_text(json.dumps(doc))
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "ghost[red]X[/red]" in result.output


def test_nameless_steps_are_labeled_by_kind_in_explain_and_query(tmp_path):
    # `source` is LangGraph checkpoint metadata (loop/input/update), not what the step is;
    # every other renderer labels a nameless step by its kind.
    steps = [
        Step(step_id="n0", trace_id="N", seq=0, kind=StepKind.CHAIN),
        Step(step_id="n1", trace_id="N", seq=1, kind=StepKind.TOOL, status=StepStatus.ERROR),
    ]
    nt = normalize(RawTrace(trace=Trace(trace_id="N", source_kind="x"), steps=steps,
                            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="n1", dst="n0")]))
    path = tmp_path / "n.json"
    artifact.save_atomic(nt, path)
    explained = runner.invoke(app, ["explain", str(path), "n1"])
    queried = runner.invoke(app, ["query", "tool-failure", str(path), "--explain"])
    for result in (explained, queried):
        assert "← CHAIN (step 0)" in result.output, result.output
        assert "← loop" not in result.output


def test_px_floor_message_follows_the_floor_constant(monkeypatch):
    monkeypatch.setattr(cli_mod, "_PX_MIN_VERSION", (9, 9, 9))
    monkeypatch.setattr(cli_mod, "_px_run", lambda *a, **k: "1.8.1\n")
    with pytest.raises(cli_mod._PxError, match=r"px >= 9\.9\.9"):
        cli_mod._px_version()


@pytest.mark.parametrize("name", ["memo[bold].md", "memo[/].md"])
def test_output_paths_are_printed_literally(tmp_path, name):
    # A path is operator text, not markup: `[/]` used to raise MarkupError *after* the file
    # was written, so the command reported failure for work it had done.
    source = _named_chain(tmp_path / "a.json", "P", "root", "leaf")
    out = tmp_path / name
    results = [
        runner.invoke(app, ["export-incident-memo", str(source), "--out", str(out)]),
        runner.invoke(app, ["analyze", str(source), "--json-out", str(tmp_path / ("r" + name))]),
        runner.invoke(app, ["export-review-candidates", "tool-failure", str(source), "--out", str(tmp_path / ("c" + name))]),
    ]
    for result in results:
        assert result.exit_code == 0, result.output
        assert name in result.output.replace("\n", "")


def test_px_read_commands_are_exact(monkeypatch):
    # The argv tracegraph hands to px is part of the read-only contract with that CLI;
    # pin it exactly rather than by substring.
    seen = []

    def failing_json(command, *, action):
        seen.append(command)
        raise cli_mod._PxError("stop")

    monkeypatch.setattr(cli_mod, "_px_json", failing_json)
    for call in (lambda: cli_mod._px_trace("abc", project="proj"),
                 lambda: cli_mod._px_latest_trace(project=None)):
        with pytest.raises(cli_mod._PxError):
            call()

    def empty_json(command, *, action):
        seen.append(command)
        return []

    monkeypatch.setattr(cli_mod, "_px_json", empty_json)
    monkeypatch.setattr(cli_mod, "_px_version", lambda: "1.8.1")
    result = runner.invoke(app, ["phoenix", "doctor", "--project", "proj"])
    assert result.exit_code == 0, result.output
    assert seen == [
        ["px", "trace", "get", "abc", "--include-annotations", "--format", "raw", "--no-progress", "--project", "proj"],
        ["px", "trace", "list", "--limit", "20", "--include-annotations", "--format", "raw", "--no-progress"],
        ["px", "project", "list", "--limit", "1", "--format", "raw", "--no-progress"],
        ["px", "trace", "list", "--limit", "1", "--format", "raw", "--no-progress", "--project", "proj"],
    ]


# Investigation guidance must navigate the exact evidence without changing analysis data.
def _investigation_artifact(path, *, count=2, source_kind="fixture", control_id=False, ok=False):
    steps = [Step(
        step_id=("ns:ok:\x1b[31mhidden" if control_id else f"--ns:ok:failure-{i}"),
        trace_id="investigation", seq=i, name="private prompt body",
        kind=StepKind.TOOL, status=StepStatus.OK if ok else StepStatus.ERROR,
    ) for i in range(count)]
    nt = normalize(RawTrace(
        trace=Trace(trace_id="investigation", source_kind=source_kind),
        steps=steps, causal_edges=[],
    ))
    artifact.save_atomic(nt, path)
    return nt


def _investigation_commands(output):
    import shlex
    return [shlex.split(line.strip()) for line in output.splitlines()
            if line.strip().startswith("tracegraph ")]


def test_investigation_artifact_commands_navigate_exact_ids(tmp_path, monkeypatch):
    # These are artifact bytes even though the producer source_kind says Phoenix.
    path = tmp_path / "a:ok:b 'quote' $HOME;[bold].json"
    nt = _investigation_artifact(path, source_kind="phoenix_cli")
    reads = []
    original = cli_mod._read_source

    def read(source):
        reads.append(source)
        return original(source)

    monkeypatch.setattr(cli_mod, "_read_source", read)
    result = runner.invoke(app, ["analyze", str(path)])
    assert result.exit_code == 0, result.output
    assert reads == [str(path)]  # provenance comes from the original parse
    assert "private prompt body" not in result.output
    commands = _investigation_commands(result.output)
    assert len(commands) == 2
    assert {c[-1] for c in commands} == {s.step_id for s in nt.steps}
    for command in commands:
        assert command[:3] == ["tracegraph", "explain", "--"]
        assert command[3] == str(path)
        explained = runner.invoke(app, command[1:])
        assert explained.exit_code == 0, explained.output
    assert "Template only" not in result.output


@pytest.mark.parametrize("saved", [False, True])
def test_investigation_truncation_matches_report_storage(tmp_path, saved):
    from tracegraph.analysis.diagnose import analyze, dumps
    path = tmp_path / "trace.json"
    nt = _investigation_artifact(path)
    out = tmp_path / "report.json"
    args = ["analyze", str(path), "--limit", "1"]
    if saved:
        args += ["--json-out", str(out)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "1 more failure candidate(s) not shown" in flat
    assert "--limit 2" in flat
    assert "in JSON report" not in flat
    assert ("Saved JSON report:" in flat) is saved
    assert ("No JSON report saved" in flat) is not saved
    assert out.exists() is saved
    if saved:
        assert out.read_text() == dumps(analyze(nt))
    assert len(_investigation_commands(result.output)) == 1


@pytest.mark.parametrize("stdin", [False, True])
def test_investigation_phoenix_export_requires_explicit_ingest(tmp_path, stdin):
    path = tmp_path / "p:x: 'q' $HOME;[x].json"
    payload = json.dumps(_phoenix_export())
    path.write_text(payload)
    result = runner.invoke(app, ["analyze", "-" if stdin else str(path)],
                           input=payload if stdin else None)
    assert result.exit_code == 0, result.output
    commands = _investigation_commands(result.output)
    assert commands[0][:2] == ["tracegraph", "ingest-phoenix"]
    assert "--file=" + ("-" if stdin else str(path)) in commands[0]
    assert "--trace=phoenix-1" in commands[0]
    assert "--out=CHOOSE_NEW_ARTIFACT_PATH" in commands[0]
    assert "Template only" in result.output
    assert "parent_only" in result.output
    assert "re-fetch" not in result.output
    assert ("Replay the original Phoenix" in result.output) is stdin
    # Resolve the explicitly labelled template, ingest, then execute its explain commands.
    out = tmp_path / "saved.json"
    ingest = [a.replace("CHOOSE_NEW_ARTIFACT_PATH", str(out)) for a in commands[0][1:]]
    ingested = runner.invoke(app, ingest, input=payload if stdin else None)
    assert ingested.exit_code == 0, ingested.output
    for command in commands[1:]:
        args = [a.replace("CHOOSE_NEW_ARTIFACT_PATH", str(out)) for a in command[1:]]
        assert runner.invoke(app, args).exit_code == 0


def test_investigation_artifact_stdin_requires_replay(tmp_path):
    path = tmp_path / "input.json"
    _investigation_artifact(path, count=1)
    result = runner.invoke(app, ["analyze", "-"], input=path.read_text())
    assert result.exit_code == 0, result.output
    assert "Replay the original artifact stream" in result.output
    assert "ingest-phoenix" not in result.output
    assert "CHOOSE_NEW_ARTIFACT_PATH" in result.output
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("saved", [False, True])
def test_investigation_live_phoenix_uses_selected_trace_and_project(tmp_path, monkeypatch, saved):
    from tracegraph.adapters import PhoenixExportAdapter
    nt = normalize(PhoenixExportAdapter(_phoenix_export("trace:ok:1")).ingest("trace:ok:1"))
    monkeypatch.setattr(cli_mod, "_px_version", lambda: "1.8.1")
    monkeypatch.setattr(cli_mod, "_px_latest_trace", lambda **kw: (nt, True, 20))
    out = tmp_path / "saved.json"
    args = ["phoenix", "diagnose", "--project", "a:ok: 'project';$HOME"]
    if saved:
        args += ["--save-artifact", str(out)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    commands = _investigation_commands(result.output)
    assert out.exists() is saved
    if saved:
        assert all(c[:3] == ["tracegraph", "explain", "--"] for c in commands)
        assert all(c[3] == str(out) for c in commands)
    else:
        assert commands[0][:3] == ["tracegraph", "phoenix", "diagnose"]
        assert "--project=a:ok: 'project';$HOME" in commands[0]
        assert commands[0][-2:] == ["--", "trace:ok:1"]
        assert "upstream data may have changed" in result.output


@pytest.mark.parametrize("command", ["analyze", "phoenix"])
def test_investigation_does_not_claim_failed_save_succeeded(tmp_path, monkeypatch, command):
    path = tmp_path / "input.json"
    nt = _investigation_artifact(path)
    # Replacing an existing directory is a portable write failure.
    destination = tmp_path / "directory"
    destination.mkdir()
    if command == "analyze":
        args = ["analyze", str(path), "--json-out", str(destination)]
    else:
        monkeypatch.setattr(cli_mod, "_px_version", lambda: "1.8.1")
        monkeypatch.setattr(cli_mod, "_px_trace", lambda *a, **kw: nt)
        args = ["phoenix", "diagnose", "id", "--save-artifact", str(destination)]
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "Next investigation steps" not in result.output
    assert "Saved JSON report:" not in result.output
    assert "step_id:" in result.output  # analysis still renders before the failed write


def test_investigation_controls_are_escaped_not_copyable(tmp_path):
    path = tmp_path / "input.json"
    _investigation_artifact(path, count=1, control_id=True)
    result = runner.invoke(app, ["analyze", str(path)])
    assert result.exit_code == 0, result.output
    assert "\\u001b" in result.output
    assert "ns:ok:" in result.output
    assert "\x1b[31mhidden" not in result.output
    assert "no copyable shell command" in result.output
    assert not _investigation_commands(result.output)


def test_investigation_no_error_has_no_spurious_actions(tmp_path):
    path = tmp_path / "input.json"
    _investigation_artifact(path, count=1, ok=True)
    result = runner.invoke(app, ["analyze", str(path)])
    assert result.exit_code == 0, result.output
    assert "Next investigation steps" not in result.output
    assert not _investigation_commands(result.output)


@pytest.mark.parametrize("ok", [False, True])
def test_investigation_literal_report_and_long_id_at_narrow_width(tmp_path, monkeypatch, ok):
    from rich.console import Console
    monkeypatch.setattr(cli_mod, "console", Console(width=40, force_terminal=False))
    path = tmp_path / "input.json"
    nt = _investigation_artifact(path, count=1, ok=ok)
    long_id = "ns:ok:" + "segment-" * 20 + "end"
    nt.steps[0].step_id = long_id
    # Re-normalize so the derived BELONGS_TO edge uses the new ID.
    nt = normalize(RawTrace(trace=nt.trace, steps=nt.steps, causal_edges=[]))
    artifact.save_atomic(nt, path)
    out = tmp_path / ("report:ok:" + "long-" * 20 + ".json")
    result = runner.invoke(app, ["analyze", str(path), "--json-out", str(out)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("Saved JSON report:" in line and str(out) in line for line in lines)
    assert result.output.count(str(out)) == 1
    if not ok:
        assert '    step_id: "' + long_id + '"' in lines
        assert _investigation_commands(result.output)[0][-1] == long_id


@pytest.mark.parametrize("phoenix", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "/dev/stdin", "//dev/stdin", "/dev/../dev/stdin", "/dev/stdin/", "/dev/stdin/.",
        "/proc/self/fd/0", "/proc/self/fd/0/", "/proc/self/fd/0/.", "/proc/thread-self/fd/0",
        "symlink", "chain", "dir-symlink", "dotdot-through-symlink", "dirfd",
    ],
)
def test_investigation_redirected_stdin_alias_requires_replay(tmp_path, phoenix, source):
    from pathlib import Path
    pass_fds = ()
    if source == "dirfd":
        # A directory descriptor: the alias is an intermediate component, not the leaf.
        dir_fd = os.open(tmp_path, os.O_RDONLY)
        pass_fds = (dir_fd,)
        source = f"/proc/self/fd/{dir_fd}/input.json"
        if not Path(f"/proc/self/fd/{dir_fd}").is_dir():
            os.close(dir_fd)
            pytest.skip("requires /proc directory descriptors")
    if source == "symlink":
        # A user-made link to the alias: realpath would resolve it to the backing file.
        (tmp_path / "trace.json").symlink_to("/dev/stdin")
        source = str(tmp_path / "trace.json")
    elif source == "chain":
        (tmp_path / "hop.json").symlink_to("/dev/stdin")
        (tmp_path / "trace.json").symlink_to(tmp_path / "hop.json")
        source = str(tmp_path / "trace.json")
    elif source == "dir-symlink":
        # The alias spelling is only visible after resolving the directory.
        (tmp_path / "descriptors").symlink_to("/dev/fd")
        source = str(tmp_path / "descriptors" / "0")
    elif source == "dotdot-through-symlink":
        # ".." applies to the link target's parent, so this is target/trace.json.
        (tmp_path / "target" / "deep").mkdir(parents=True)
        (tmp_path / "target" / "trace.json").symlink_to("/dev/stdin")
        (tmp_path / "bridge").symlink_to(tmp_path / "target" / "deep")
        source = str(tmp_path / "bridge" / ".." / "trace.json")
    path = tmp_path / "input.json"
    _investigation_artifact(path, count=1)
    if phoenix:
        path.write_text(json.dumps(_phoenix_export()))
    if not pass_fds and not Path(source).exists():
        pytest.skip("requires stdin descriptor alias")
    # A real regular-file stdin redirect matters: is_file() is true on these aliases,
    # and Linux realpath() follows /proc/self/fd/0 all the way to the input file.
    with path.open("rb") as stdin:
        result = subprocess.run(
            [sys.executable, "-c", "from tracegraph.cli import app; app()", "analyze", source],
            stdin=stdin, capture_output=True, text=True, timeout=30, pass_fds=pass_fds,
        )
    for fd in pass_fds:
        os.close(fd)
    assert result.returncode == 0, result.stderr
    commands = _investigation_commands(result.stdout)
    assert commands
    assert all(source not in arg for command in commands for arg in command)
    assert "Template only" in result.stdout
    if phoenix:
        assert "--file=-" in commands[0]
    else:
        assert "consumed stream cannot be recovered" in " ".join(result.stdout.split())


def test_investigation_dev_shm_regular_file_keeps_direct_command(tmp_path):
    # Only descriptor aliases are process-bound; a regular file under /dev/ is reusable.
    from pathlib import Path
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK)):
        pytest.skip("requires a writable /dev/shm")
    path = shm / f"tracegraph-test-{os.getpid()}.json"
    try:
        _investigation_artifact(path, count=1)
        result = runner.invoke(app, ["analyze", str(path)])
        assert result.exit_code == 0, result.output
        commands = _investigation_commands(result.output)
        assert commands and str(path) in commands[0]
        assert "Template only" not in result.output
    finally:
        path.unlink(missing_ok=True)


def test_investigation_dotdot_through_symlink_to_regular_file_keeps_command(tmp_path):
    # The mirror of the replay case: lexically, "bridge/../trace.json" is tmp_path/trace.json,
    # which is the alias; on the filesystem it is target/trace.json, a regular artifact.
    from pathlib import Path
    (tmp_path / "trace.json").symlink_to("/dev/stdin")
    (tmp_path / "target" / "deep").mkdir(parents=True)
    _investigation_artifact(tmp_path / "target" / "trace.json", count=1)
    (tmp_path / "bridge").symlink_to(tmp_path / "target" / "deep")
    source = str(tmp_path / "bridge" / ".." / "trace.json")
    assert Path(source).is_file()
    result = runner.invoke(app, ["analyze", source])
    assert result.exit_code == 0, result.output
    commands = _investigation_commands(result.output)
    assert commands and source in commands[0]
    assert "Template only" not in result.output


def test_is_descriptor_alias_spellings():
    from tracegraph.cli import _is_descriptor_alias
    assert _is_descriptor_alias("//proc/1234/fd/3")
    assert _is_descriptor_alias("/proc/1234/fd/3/")
    assert _is_descriptor_alias("/proc/1234/fd/3/./trace.json")
    assert _is_descriptor_alias("/proc/1234/task/5678/fd/3")
    assert _is_descriptor_alias("/dev/stdout")
    assert _is_descriptor_alias("/dev/stdout/")
    assert not _is_descriptor_alias("/dev/shm/trace.json")
    assert not _is_descriptor_alias("/dev/stdin.json")
    assert not _is_descriptor_alias("/devices/fd/0")
    assert not _is_descriptor_alias("/proc/self/cmdline")


@pytest.mark.parametrize("phoenix", [False, True])
@pytest.mark.parametrize("regular_file", [False, True])
def test_investigation_consumed_pipe_requires_replay(tmp_path, phoenix, regular_file):
    from pathlib import Path
    path = tmp_path / "input.json"
    _investigation_artifact(path, count=1)
    payload = json.dumps(_phoenix_export()).encode() if phoenix else path.read_bytes()
    if regular_file:
        path.write_bytes(payload)
        read_fd = os.open(path, os.O_RDONLY)
        write_fd = None
    else:
        read_fd, write_fd = os.pipe()
    source = f"/dev/fd/{read_fd}"
    try:
        if not Path(source).exists():
            pytest.skip("requires /dev/fd")
        if write_fd is not None:
            os.write(write_fd, payload)
            os.close(write_fd)
            write_fd = None
        result = runner.invoke(app, ["analyze", source])
        assert result.exit_code == 0, result.output
        commands = _investigation_commands(result.output)
        assert all(source not in arg for command in commands for arg in command)
        assert "Template only" in result.output
        assert "re-fetch" not in result.output
        if phoenix:
            assert "--file=-" in commands[0]
        else:
            assert "consumed stream cannot be recovered" in " ".join(result.output.split())
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


# --- analyze source routing: artifact / OTLP / Phoenix by shape (#57) --------------------

from pathlib import Path  # noqa: E402  (section-local; the file imports Path inside tests)

_SYNCMILL_OTLP = sorted(Path("tests/fixtures/syncmill").glob("*.otlp.json"))


def _flat(output):
    """Error panels wrap at 80 columns with box-drawing borders; compare on the words only."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def _otlp_spans(trace_id, *, failed=True, prefix="demo"):
    spans = [{"traceId": trace_id, "spanId": f"{prefix}-first", "name": f"{prefix}::tool",
              "status": {"code": 1}, "attributes": {"openinference.span.kind": "TOOL"}}]
    if failed:
        spans.append({"traceId": trace_id, "spanId": f"{prefix}-last", "parentSpanId": f"{prefix}-first",
                      "name": f"{prefix}::tool", "status": {"code": 2, "message": "PRIVATE BODY"},
                      "attributes": {"openinference.span.kind": "TOOL"}})
    return spans


def _otlp_export(*trace_ids, failed=True):
    spans = [span for tid in (trace_ids or ("otlp-1",)) for span in _otlp_spans(tid, failed=failed, prefix=tid)]
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def _analysis_artifact_text(path):
    _write_linear_trace(path, "art-1", ("agent", StepKind.AGENT, StepStatus.ERROR))
    return path.read_text()


_ROUTING_OK = {
    "artifact": lambda tmp: (_analysis_artifact_text(tmp / "seed.json"), "art-1"),
    "phoenix-dict": lambda tmp: (json.dumps(_phoenix_export("px-1")), "px-1"),
    "phoenix-root-traces": lambda tmp: (json.dumps({"traces": [_phoenix_export("px-1")]}), "px-1"),
    "phoenix-root-data": lambda tmp: (json.dumps({"data": [_phoenix_export("px-1")]}), "px-1"),
    "phoenix-list": lambda tmp: (json.dumps([_phoenix_export("px-1")]), "px-1"),
    "otlp-dict": lambda tmp: (json.dumps(_otlp_export("otlp-1")), "otlp-1"),
    "otlp-list": lambda tmp: (json.dumps([_otlp_export("otlp-1"), {"resourceSpans": []}]), "otlp-1"),
    "otlp-jsonl": lambda tmp: (
        json.dumps(_otlp_export("otlp-1")) + "\n" + json.dumps({"resource_spans": []}) + "\n", "otlp-1"),
}


@pytest.mark.parametrize("shape", sorted(_ROUTING_OK))
def test_analyze_routes_each_supported_shape(tmp_path, shape):
    text, trace_id = _ROUTING_OK[shape](tmp_path)
    source = tmp_path / "input.json"
    source.write_text(text)
    result = runner.invoke(app, ["analyze", str(source)])
    assert result.exit_code == 0, result.output
    assert trace_id in result.output and "What failed" in result.output


_ROUTING_REJECTED = {
    "not-json": ("{not json", "not valid JSON"),
    "jsonl-bad-line": (json.dumps(_otlp_export()) + "\n{nope\n", "not valid JSON"),
    "empty-object": ("{}", "unsupported shape"),
    "empty-objects-jsonl": ("{}\n{}\n", "unsupported shape"),
    "empty-list": ("[]", "unsupported shape"),
    "scalar": ("1", "unsupported shape"),
    "unknown-keys": (json.dumps({"foo": 1}), "unsupported shape"),
    "non-list-phoenix-key": (json.dumps({"data": {}}), "unsupported shape"),
    "wrapper-list-traces": (json.dumps([{"traces": [_phoenix_export()]}]), "unsupported shape"),
    "wrapper-list-data": (json.dumps([{"data": [_phoenix_export()]}]), "unsupported shape"),
    "jsonl-phoenix-lines": (json.dumps(_phoenix_export("a")) + "\n" + json.dumps(_phoenix_export("b")) + "\n",
                            "JSON Lines input is read as an OTLP"),
    "hybrid-dict": (json.dumps({**_otlp_export(), **_phoenix_export()}), "ambiguous input"),
    "hybrid-list-item": (json.dumps([{**_otlp_export(), **_phoenix_export()}]), "ambiguous input"),
    "hybrid-jsonl-line": (json.dumps({**_otlp_export(), **_phoenix_export()}) + "\n"
                          + json.dumps({"resourceSpans": []}) + "\n", "ambiguous input"),
    "hybrid-jsonl-traces-envelope": (json.dumps({**_otlp_export(), "traces": [_phoenix_export()]}) + "\n"
                                     + json.dumps({"resourceSpans": []}) + "\n", "ambiguous input"),
    "hybrid-jsonl-data-envelope": (json.dumps({**_otlp_export(), "data": [_phoenix_export()]}) + "\n"
                                   + json.dumps({"resourceSpans": []}) + "\n", "ambiguous input"),
    "hybrid-dict-traces-envelope": (json.dumps({**_otlp_export(), "traces": [_phoenix_export()]}), "ambiguous input"),
    "envelope-traces-hybrid-record": (json.dumps({"traces": [{**_otlp_export(), **_phoenix_export()}]}),
                                      "ambiguous input"),
    "envelope-data-hybrid-record": (json.dumps({"data": [{**_otlp_export(), **_phoenix_export()}]}),
                                    "ambiguous input"),
    "envelope-with-otlp-record": (json.dumps({"traces": [_phoenix_export(), _otlp_export()]}), "ambiguous input"),
    "artifact-in-list": (json.dumps([{"schema_version": 99, **_otlp_export()}]), "must be the whole document"),
    "artifact-in-jsonl": (json.dumps({"schema_version": 99, **_otlp_export()}) + "\n"
                          + json.dumps({"resourceSpans": []}) + "\n", "must be the whole document"),
    "artifact-in-envelope": (json.dumps({"traces": [{"schema_version": 99, **_phoenix_export()}]}),
                             "must be the whole document"),
    "mixed-list": (json.dumps([_otlp_export(), _phoenix_export()]), "ambiguous input"),
    "mixed-jsonl": (json.dumps(_otlp_export()) + "\n" + json.dumps(_phoenix_export()) + "\n", "ambiguous input"),
}


@pytest.mark.parametrize("shape", sorted(_ROUTING_REJECTED))
def test_analyze_rejects_unsupported_and_colliding_shapes(tmp_path, shape):
    text, expected = _ROUTING_REJECTED[shape]
    source = tmp_path / "input.json"
    source.write_text(text)
    result = runner.invoke(app, ["analyze", str(source)])
    assert result.exit_code == 2, result.output
    flat = _flat(result.output)
    assert "cannot load analysis input" in flat and expected in flat, flat
    # Never the old behaviour: an unknown payload tried as Phoenix and failing there.
    assert "Phoenix export must be" not in flat and "every Phoenix trace" not in flat


def test_analyze_collision_advice_is_executable(tmp_path):
    hybrid = tmp_path / "hybrid.json"
    hybrid.write_text(json.dumps({**_otlp_export("otlp-1"), **_phoenix_export("px-1")}))
    assert runner.invoke(app, ["analyze", str(hybrid)]).exit_code == 2
    as_otlp = runner.invoke(app, ["ingest-otlp", "--file", str(hybrid), "--out", str(tmp_path / "o.json")])
    as_phoenix = runner.invoke(app, ["ingest-phoenix", "--file", str(hybrid), "--out", str(tmp_path / "p.json")])
    assert as_otlp.exit_code == 0 and as_phoenix.exit_code == 0, (as_otlp.output, as_phoenix.output)
    assert artifact.load(tmp_path / "o.json").trace.trace_id == "otlp-1"
    assert artifact.load(tmp_path / "p.json").trace.trace_id == "px-1"
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps([_otlp_export("otlp-1"), _phoenix_export("px-1")]))
    flat = _flat(runner.invoke(app, ["analyze", str(mixed)]).output)
    assert "ambiguous input" in flat and "extract the records of one format" in flat
    (tmp_path / "only-otlp.json").write_text(json.dumps(_otlp_export("otlp-1")))
    only_otlp = runner.invoke(app, ["ingest-otlp", "--file", str(tmp_path / "only-otlp.json"),
                                    "--out", str(tmp_path / "m.json")])
    assert only_otlp.exit_code == 0, only_otlp.output
    assert artifact.load(tmp_path / "m.json").trace.trace_id == "otlp-1"


@pytest.mark.parametrize("jsonl", [False, True])
def test_analyze_container_collision_advice_holds_with_invalid_records(tmp_path, jsonl):
    # A null record makes whole-file ingest-otlp fail, so the advice must be extraction.
    records = [_otlp_export("otlp-1"), _phoenix_export("px-1"), None]
    source = tmp_path / "records.json"
    source.write_text("\n".join(json.dumps(r) for r in records) + "\n" if jsonl else json.dumps(records))
    flat = _flat(runner.invoke(app, ["analyze", str(source)]).output)
    assert "ambiguous input" in flat and "extract the records of one format" in flat, flat
    assert "reads only" not in flat


@pytest.mark.parametrize("jsonl", [False, True])
def test_analyze_otlp_container_validity_matches_ingest_otlp(tmp_path, jsonl):
    # Record validity is the adapter's call: the direct path accepts and rejects what
    # ingest-otlp does, so an empty object beside an OTLP document is fine for both and a
    # null record is refused by both.
    for records, ok in (([{}, _otlp_export("otlp-1")], True), ([_otlp_export("otlp-1"), None], False)):
        source = tmp_path / "records.json"
        source.write_text("\n".join(json.dumps(r) for r in records) + "\n" if jsonl else json.dumps(records))
        direct = runner.invoke(app, ["analyze", str(source)])
        ingested = runner.invoke(app, ["ingest-otlp", "--file", str(source), "--out", str(tmp_path / "a.json")])
        assert (direct.exit_code == 0) is ok and (ingested.exit_code == 0) is ok, (direct.output, ingested.output)
        if ok:
            assert "otlp-1" in direct.output


def test_analyze_otlp_without_traces_says_so(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"resourceSpans": []}))
    flat = _flat(runner.invoke(app, ["analyze", str(source)]).output)
    assert "holds no traces" in flat and "pass --trace" not in flat, flat


@pytest.mark.parametrize("envelope", ["traces", "data"])
def test_analyze_envelope_collision_advice_is_executable(tmp_path, envelope):
    # ingest-otlp cannot see resourceSpans nested in a Phoenix envelope, so the advice must be
    # to extract the record, and extracting it must work.
    hybrid = {**_otlp_export("otlp-1"), **_phoenix_export("px-1")}
    source = tmp_path / "env.json"
    source.write_text(json.dumps({envelope: [hybrid]}))
    flat = _flat(runner.invoke(app, ["analyze", str(source)]).output)
    assert "ambiguous input" in flat and "extract the records of one format" in flat
    assert runner.invoke(app, ["ingest-otlp", "--file", str(source), "--out", str(tmp_path / "no.json")]).exit_code == 2
    extracted = tmp_path / "record.json"
    extracted.write_text(json.dumps(hybrid))
    assert runner.invoke(app, ["ingest-otlp", "--file", str(extracted), "--out", str(tmp_path / "o.json")]).exit_code == 0
    assert runner.invoke(app, ["ingest-phoenix", "--file", str(extracted), "--out", str(tmp_path / "p.json")]).exit_code == 0
    assert artifact.load(tmp_path / "o.json").trace.trace_id == "otlp-1"
    assert artifact.load(tmp_path / "p.json").trace.trace_id == "px-1"
    source.write_text(json.dumps({envelope: [_phoenix_export("px-1"), _otlp_export("otlp-1")]}))
    flat = _flat(runner.invoke(app, ["analyze", str(source)]).output)
    assert "ambiguous input" in flat and "extract the records of one format" in flat
    assert "reads only" not in flat


def test_analyze_keeps_artifact_validation_without_adapter_fallback(tmp_path):
    good = json.loads(_analysis_artifact_text(tmp_path / "seed.json"))
    cases = {
        "unsupported artifact schema_version": {**good, "schema_version": 99},
        'missing the required "trace"': {"schema_version": good["schema_version"]},
        "disagrees with its steps": {**good, "trace": {**good["trace"],
                                                       "trace": {**good["trace"]["trace"], "status": "ok"}}},
    }
    for expected, payload in cases.items():
        source = tmp_path / "bad.json"
        source.write_text(json.dumps(payload))
        result = runner.invoke(app, ["analyze", str(source)])
        flat = _flat(result.output)
        assert result.exit_code == 2 and expected in flat, flat
        assert "OTLP" not in flat and "Phoenix" not in flat


@pytest.mark.parametrize("fixture", _SYNCMILL_OTLP, ids=lambda p: p.stem)
def test_analyze_direct_otlp_equals_ingest_then_analyze(tmp_path, fixture):
    direct = runner.invoke(app, ["analyze", str(fixture), "--json-out", str(tmp_path / "direct.json")])
    assert direct.exit_code == 0, direct.output
    ingested = runner.invoke(app, ["ingest-otlp", "--file", str(fixture), "--out", str(tmp_path / "art.json")])
    assert ingested.exit_code == 0, ingested.output
    via_artifact = runner.invoke(app, ["analyze", str(tmp_path / "art.json"), "--json-out", str(tmp_path / "via.json")])
    assert via_artifact.exit_code == 0, via_artifact.output
    assert (tmp_path / "direct.json").read_bytes() == (tmp_path / "via.json").read_bytes()
    if fixture.name == "compete-gate-reject.otlp.json":
        assert "gate-failure-after-success" in direct.output  # UNSET read as ok, as ingest-otlp does
    if fixture.name == "route-fallback.otlp.json":
        assert "declared_dag" in direct.output and "links_preserved=True" in direct.output


@pytest.mark.parametrize("via_stdin", [False, True])
def test_analyze_direct_otlp_writes_nothing_but_the_requested_report(tmp_path, monkeypatch, via_stdin):
    monkeypatch.chdir(tmp_path)
    text = json.dumps(_otlp_export("otlp-1"))
    Path("input.json").write_text(text)
    args = ["analyze", "-"] if via_stdin else ["analyze", "input.json"]
    result = runner.invoke(app, args, input=text if via_stdin else None)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.json"]
    assert Path("input.json").read_text() == text
    result = runner.invoke(app, [*args, "--json-out", "report.json"], input=text if via_stdin else None)
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.json", "report.json"]


def test_analyze_multi_trace_otlp_requires_explicit_selection(tmp_path):
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps(_otlp_export("otlp-a", "otlp-b")))
    result = runner.invoke(app, ["analyze", str(multi)])
    flat = _flat(result.output)
    assert result.exit_code == 2 and "holds 2 traces" in flat and "otlp-a, otlp-b" in flat, flat
    result = runner.invoke(app, ["analyze", str(multi), "--trace", "otlp-b"])
    assert result.exit_code == 0 and "otlp-b" in result.output, result.output
    assert runner.invoke(app, ["analyze", str(multi), "--trace", "missing"]).exit_code == 2


def test_analyze_otlp_baseline_selection_and_limit(tmp_path, monkeypatch):
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_otlp_export("otlp-cur")))
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps(_otlp_export("otlp-a", "otlp-b")))  # 4 spans: larger than current
    result = runner.invoke(app, ["analyze", str(current), "--baseline", str(multi)])
    assert result.exit_code == 2 and "holds 2 traces" in _flat(result.output), result.output
    result = runner.invoke(app, ["analyze", str(current), "--baseline", str(multi), "--baseline-trace", "otlp-a"])
    assert result.exit_code == 0 and "baseline" in result.output.lower(), result.output
    assert runner.invoke(app, ["analyze", str(current), "--baseline", str(multi),
                               "--baseline-trace", "missing"]).exit_code == 2
    assert multi.stat().st_size > current.stat().st_size
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(current.stat().st_size))
    result = runner.invoke(app, ["analyze", str(current), "--baseline", str(multi), "--baseline-trace", "otlp-a"])
    assert result.exit_code == 2 and "byte limit" in _flat(result.output), result.output
    result = runner.invoke(app, ["analyze", str(multi), "--trace", "otlp-a"])
    assert result.exit_code == 2 and "byte limit" in _flat(result.output), result.output


def _panel_message(output):
    """The text inside the error panel, one line per panel row, borders stripped."""
    return " ".join(line.strip("│ ") for line in output.splitlines() if line.startswith("│"))


@pytest.mark.parametrize("option", ["--trace", "--baseline-trace"])
def test_analyze_rejects_a_selector_naming_another_trace_on_an_artifact(tmp_path, monkeypatch, option):
    monkeypatch.chdir(tmp_path)
    _write_linear_trace(tmp_path / "art.json", "art-1", ("agent", StepKind.AGENT, StepStatus.ERROR))
    Path("report.json").write_text("previous report\n")
    args = ["analyze", "art.json", "--baseline", "art.json", option, "other", "--json-out", "report.json"]
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert _panel_message(result.output) == f"Invalid value for {option}: art.json holds trace 'art-1', not 'other'"
    assert Path("report.json").read_text() == "previous report\n"


def test_analyze_rejects_a_mismatched_selector_on_an_artifact_from_stdin(tmp_path):
    text = _analysis_artifact_text(tmp_path / "art.json")
    result = runner.invoke(app, ["analyze", "-", "--trace", "other"], input=text)
    assert result.exit_code == 2, result.output
    assert _panel_message(result.output) == "Invalid value for --trace: - holds trace 'art-1', not 'other'"


def test_analyze_accepts_a_selector_naming_the_artifacts_own_trace(tmp_path):
    art = tmp_path / "art.json"
    _write_linear_trace(art, "art-1", ("agent", StepKind.AGENT, StepStatus.ERROR))
    plain, selected = tmp_path / "plain.json", tmp_path / "selected.json"
    result = runner.invoke(app, ["analyze", str(art), "--baseline", str(art), "--json-out", str(plain)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["analyze", str(art), "--trace", "art-1", "--baseline", str(art),
                                 "--baseline-trace", "art-1", "--json-out", str(selected)])
    assert result.exit_code == 0, result.output
    assert selected.read_bytes() == plain.read_bytes()


@pytest.mark.parametrize("via_stdin", [False, True])
def test_analyze_direct_otlp_next_steps_name_ingest_otlp(tmp_path, via_stdin):
    text = json.dumps(_otlp_export("otlp-1"))
    source = tmp_path / "input.json"
    source.write_text(text)
    args = ["analyze", "-"] if via_stdin else ["analyze", str(source)]
    result = runner.invoke(app, args, input=text if via_stdin else None)
    assert result.exit_code == 0, result.output
    commands = _investigation_commands(result.output)
    assert commands[0][:2] == ["tracegraph", "ingest-otlp"]
    assert "--trace=otlp-1" in commands[0] and "--out=CHOOSE_NEW_ARTIFACT_PATH" in commands[0]
    assert ("--file=-" if via_stdin else f"--file={source}") in commands[0]
    assert ("Replay the original OTLP export" in result.output) is via_stdin
    assert "Phoenix" not in result.output
    assert commands[-1][:3] == ["tracegraph", "explain", "--"]


def test_ingest_otlp_reads_stdin_and_ingest_phoenix_keeps_its_inputs(tmp_path):
    out = tmp_path / "from-stdin.json"
    result = runner.invoke(app, ["ingest-otlp", "--file", "-", "--out", str(out)],
                           input=json.dumps(_otlp_export("otlp-1")))
    assert result.exit_code == 0, result.output
    assert artifact.load(out).trace.trace_id == "otlp-1"
    otlp_file = tmp_path / "otlp.json"
    otlp_file.write_text(json.dumps(_otlp_export("otlp-1")))
    result = runner.invoke(app, ["ingest-phoenix", "--file", str(otlp_file), "--out", str(tmp_path / "x.json")])
    assert result.exit_code == 2 and "Phoenix export must be" in _flat(result.output)
    px_file = tmp_path / "px.json"
    px_file.write_text(json.dumps(_phoenix_export("px-1")))
    first = tmp_path / "px-art.json"
    assert runner.invoke(app, ["ingest-phoenix", "--file", str(px_file), "--out", str(first)]).exit_code == 0
    resaved = runner.invoke(app, ["ingest-phoenix", "--file", str(first), "--out", str(tmp_path / "px-art2.json")])
    assert resaved.exit_code == 0, resaved.output
    assert (tmp_path / "px-art2.json").read_bytes() == first.read_bytes()
    seed = tmp_path / "seed.json"
    _analysis_artifact_text(seed)
    rejected = runner.invoke(app, ["ingest-phoenix", "--file", str(seed), "--out", str(tmp_path / "y.json")])
    assert rejected.exit_code == 2 and "must contain a Phoenix CLI trace export" in _flat(rejected.output)


def _phoenix_artifact(tmp_path):
    export = tmp_path / "px.json"
    export.write_text(json.dumps(_phoenix_export("px-1")))
    art = tmp_path / "px-art.json"
    assert runner.invoke(app, ["ingest-phoenix", "--file", str(export), "--out", str(art)]).exit_code == 0
    return art


@pytest.mark.parametrize("via_stdin", [False, True])
def test_ingest_phoenix_rejects_a_selector_naming_another_trace_on_an_artifact(tmp_path, monkeypatch, via_stdin):
    monkeypatch.chdir(tmp_path)
    art = _phoenix_artifact(tmp_path)
    Path("out.json").write_text("previous artifact\n")
    source = "-" if via_stdin else "px-art.json"
    for out in (["--out", "out.json"], []):
        result = runner.invoke(app, ["ingest-phoenix", "--file", source, "--trace", "other", *out],
                               input=art.read_text() if via_stdin else None)
        assert result.exit_code == 2, result.output
        assert _panel_message(result.output) == f"Invalid value for --trace: {source} holds trace 'px-1', not 'other'"
    assert Path("out.json").read_text() == "previous artifact\n"
    assert not Path("px-1.json").exists()


def test_ingest_phoenix_accepts_a_selector_naming_the_artifacts_own_trace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    art = _phoenix_artifact(tmp_path)
    result = runner.invoke(app, ["ingest-phoenix", "--file", "px-art.json", "--trace", "px-1", "--out", "out.json"])
    assert result.exit_code == 0, result.output
    assert Path("out.json").read_bytes() == art.read_bytes()
    result = runner.invoke(app, ["ingest-phoenix", "--file", "px-art.json", "--trace", "px-1"])
    assert result.exit_code == 0, result.output
    assert Path("px-1.json").read_bytes() == art.read_bytes()


def test_ingest_phoenix_names_the_source_kind_before_a_selector_on_another_artifact(tmp_path):
    seed = tmp_path / "seed.json"
    _analysis_artifact_text(seed)
    result = runner.invoke(app, ["ingest-phoenix", "--file", str(seed), "--trace", "other",
                                 "--out", str(tmp_path / "y.json")])
    assert result.exit_code == 2
    assert _panel_message(result.output) == "Invalid value: --file must contain a Phoenix CLI trace export"


def test_fixture_pins_in_equivalence_test_are_reachable():
    names = {p.name for p in _SYNCMILL_OTLP}
    assert {"compete-gate-reject.otlp.json", "route-fallback.otlp.json"} <= names


@pytest.mark.parametrize("command", [["analyze", "-"], ["ingest-otlp", "--file", "-", "--out", "OUT"],
                                     ["ingest-phoenix", "--file", "-", "--out", "OUT"]])
def test_stdin_inputs_respect_the_byte_limit(tmp_path, monkeypatch, command):
    phoenix = command[0] == "ingest-phoenix"
    text = json.dumps(_phoenix_export("px-1") if phoenix else _otlp_export("otlp-1"))
    out = tmp_path / "out.json"
    args = [str(out) if a == "OUT" else a for a in command]
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(len(text.encode()) - 1))
    result = runner.invoke(app, args, input=text)
    assert result.exit_code == 2 and "byte limit" in _flat(result.output), result.output
    assert not out.exists()
    monkeypatch.setenv("TRACEGRAPH_MAX_ARTIFACT_BYTES", str(len(text.encode())))
    result = runner.invoke(app, args, input=text)
    assert result.exit_code == 0, result.output


# --- query: streaming pass keeps the two-phase error order (#54) ----------------------
#
# `query` loads, validates and matches one artifact at a time instead of loading every trace
# first. These pin today's precedence: every load error, then "no valid artifacts", then the
# first duplicate trace_id, then a matching failure. The injection seams are the matcher entry
# points `query` calls (`iter_matches` and `LadybugStore` methods).


def _failing_traces(tmp_path, *tids):
    paths = []
    for tid in tids:
        p = tmp_path / f"{tid}.json"
        _write_linear_trace(
            p, tid,
            ("plan", StepKind.CHAIN, StepStatus.OK),
            ("call_tool", StepKind.TOOL, StepStatus.ERROR),
        )
        paths.append(p)
    return paths


class _Injected(RuntimeError):
    pass


def _break_memory_matcher(monkeypatch, exc=None):
    # The failure is raised while the matches are being consumed, not when the matcher is called.
    from tracegraph.analysis import patterns

    def boom(*args, **kwargs):
        raise exc or _Injected("matcher broke")
        yield  # a generator: the call succeeds and the first next() raises

    monkeypatch.setattr(patterns, "iter_matches", boom)
    monkeypatch.setattr(cli_mod, "iter_matches", boom)


def test_query_matcher_failure_yields_to_a_later_explicit_load_error(tmp_path, monkeypatch):
    first, = _failing_traces(tmp_path, "A")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    _break_memory_matcher(monkeypatch)
    res = runner.invoke(app, ["query", "error", str(first), str(bad)])
    assert res.exit_code == 2, res.output
    assert "cannot load artifact" in _panel_text(res.output)


def test_query_matcher_failure_yields_to_a_later_duplicate_trace_id(tmp_path, monkeypatch):
    a, b = _failing_traces(tmp_path, "A", "B")
    dup = tmp_path / "again.json"
    dup.write_bytes(a.read_bytes())
    _break_memory_matcher(monkeypatch)
    res = runner.invoke(app, ["query", "error", str(a), str(b), str(dup)])
    assert res.exit_code == 2, res.output
    out = _panel_text(res.output)
    assert "duplicate trace_id 'A'" in out
    assert out.index("A.json") < out.index("again.json")  # first-seen path, then the repeat


@pytest.mark.parametrize("limit", [[], ["--limit", "1"]])
def test_query_matcher_failure_propagates_when_inputs_are_valid(tmp_path, monkeypatch, limit):
    paths = _failing_traces(tmp_path, "A", "B")
    _break_memory_matcher(monkeypatch)
    res = runner.invoke(app, ["query", "error", *map(str, paths), *limit])
    assert isinstance(res.exception, _Injected)
    assert str(res.exception) == "matcher broke"
    assert "match(es)" not in res.output


def test_query_interrupt_from_the_matcher_still_reports_skipped_strays(tmp_path, monkeypatch):
    # Interrupts are outside the equivalence scope: the pass can now be interrupted before a
    # later file is read. What it guarantees is that strays already skipped are reported.
    d = tmp_path / "arts"
    d.mkdir()
    (d / "0-stray.json").write_text('{"name": "x"}', encoding="utf-8")  # sorts first
    _failing_traces(d, "A")
    _break_memory_matcher(monkeypatch, KeyboardInterrupt())
    res = runner.invoke(app, ["query", "error", str(d)])
    assert res.exit_code == 130
    assert "0-stray.json" in _panel_text(res.stderr)


def test_query_memory_error_from_the_matcher_is_not_deferred(tmp_path, monkeypatch):
    # Out of the equivalence scope by design: an allocation failure must not trigger more
    # parsing and then be replaced by a later input's error.
    first, = _failing_traces(tmp_path, "A")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    loads = []
    real = cli_mod._load_artifact_evidence
    monkeypatch.setattr(
        cli_mod, "_load_artifact_evidence", lambda p: loads.append(p) or real(p)
    )
    _break_memory_matcher(monkeypatch, MemoryError())
    res = runner.invoke(app, ["query", "error", str(first), str(bad)])
    assert isinstance(res.exception, MemoryError)
    assert loads == [first]


def _ladybug_store():
    pytest.importorskip("ladybug")
    from tracegraph.store.ladybug import LadybugStore

    return LadybugStore


def test_query_ladybug_store_load_failure_yields_to_a_later_explicit_load_error(
    tmp_path, monkeypatch
):
    store_cls = _ladybug_store()
    first, = _failing_traces(tmp_path, "A")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")

    def refuse(cls, nt):
        raise ValueError("too big for ladybug")

    monkeypatch.setattr(store_cls, "from_trace", classmethod(refuse))
    res = runner.invoke(app, ["query", "error", str(first), "--backend", "ladybug"])
    assert res.exit_code == 2
    assert "cannot load trace A into the ladybug backend" in _panel_text(res.output)
    res = runner.invoke(app, ["query", "error", str(first), str(bad), "--backend", "ladybug"])
    assert res.exit_code == 2
    out = _panel_text(res.output)
    assert "cannot load artifact" in out and "ladybug backend" not in out


def test_query_ladybug_close_failure_replaces_the_matching_failure(tmp_path, monkeypatch):
    store_cls = _ladybug_store()
    paths = _failing_traces(tmp_path, "A")

    def find(self, pattern, n):
        raise _Injected("search broke")

    def close(self):
        raise OSError("close broke")

    monkeypatch.setattr(store_cls, "first_matches", find)
    monkeypatch.setattr(store_cls, "close", close)
    res = runner.invoke(app, ["query", "error", *map(str, paths), "--backend", "ladybug"])
    assert isinstance(res.exception, OSError)
    assert str(res.exception) == "close broke"
    assert isinstance(res.exception.__context__, _Injected)


def test_query_ladybug_fallback_note_only_when_every_trace_succeeds(tmp_path, monkeypatch):
    store_cls = _ladybug_store()
    paths = _failing_traces(tmp_path, "A", "B")
    preset = "repeated-agent-failure"  # unbounded gap: the ladybug store falls back to Python
    res = runner.invoke(app, ["query", preset, *map(str, paths), "--backend", "ladybug"])
    assert res.exit_code == 1  # no match, but every trace was searched
    assert "not Cypher-compilable" in res.output

    real = store_cls.first_matches
    calls = []

    def second_fails(self, pattern, n):
        calls.append(pattern)
        if len(calls) == 2:
            raise _Injected("second trace broke")
        return real(self, pattern, n)

    monkeypatch.setattr(store_cls, "first_matches", second_fails)
    res = runner.invoke(app, ["query", preset, *map(str, paths), "--backend", "ladybug"])
    assert isinstance(res.exception, _Injected)
    assert "not Cypher-compilable" not in res.output


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_query_limit_counts_every_match_of_a_trace_it_has_no_room_for(tmp_path, backend):
    # The first trace fills --limit, so the second is matched with no room left: none of its
    # matches is kept, but all of them are counted in the footer total.
    if backend == "ladybug":
        _ladybug_store()
    paths = []
    for tid, n in [("A", 6), ("B", 5)]:
        p = tmp_path / f"{tid}.json"
        _write_linear_trace(p, tid, *[("x", StepKind.AGENT, StepStatus.ERROR)] * n)
        paths.append(p)
    res = runner.invoke(app, ["query", "repeated-agent-failure", *map(str, paths),
                              "--limit", "1", "--backend", backend])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert [ln for ln in lines if ln.startswith(("A:", "B:"))] == ["A: x → x"]
    # 6·5/2 pairs in A plus 5·4/2 in B.
    assert lines[-1] == "1 match(es) across 1 trace(s) — truncated from 25"


@pytest.mark.parametrize("limit", [None, 1, 2, 3, 4, 7])
@pytest.mark.parametrize("explain", [False, True])
def test_query_pass_equals_loading_everything_then_slicing(tmp_path, limit, explain):
    from tracegraph.analysis import PRESETS

    d = tmp_path / "arts"
    d.mkdir()
    for i, n_fail in enumerate([0, 2, 1, 0, 3]):
        _write_linear_trace(
            d / f"t{i}.json", f"T{i}",
            ("plan", StepKind.CHAIN, StepStatus.OK),
            *[("call_tool", StepKind.TOOL, StepStatus.ERROR)] * n_fail,
        )
    (d / "stray.json").write_text("{}", encoding="utf-8")
    pattern = PRESETS["error"]
    backend = cli_mod.QueryBackend.MEMORY
    from tracegraph.analysis import search  # independent of the CLI's own matching helpers

    loaded = cli_mod._load_many_evidence([d])
    everything = search([x.trace for x in loaded], pattern)
    got = cli_mod._query_pass([d], pattern, backend, limit=limit, keep_traces=explain)
    assert got.n_traces == len(loaded)
    assert got.total == len(everything)
    assert got.matches == everything[:limit]
    expected_traces = {m.trace_id for m in everything[:limit]} if explain else set()
    assert set(got.traces) == expected_traces


@pytest.mark.parametrize("explain", [False, True])
def test_query_peak_memory_does_not_grow_with_the_number_of_artifacts(tmp_path, explain):
    # With a fixed --limit, only per-file metadata may grow with the corpus. Before the
    # streaming pass every parsed trace stayed alive: 300-step artifacts added ~0.9 MiB of
    # traced peak each (10 -> 30 artifacts: +18 MiB); the streaming pass adds ~1 KiB each.
    import tracemalloc

    def corpus(n):
        d = tmp_path / f"c{n}"
        d.mkdir()
        for k in range(n):
            _write_linear_trace(
                d / f"t{k:03d}.json", f"T{k:03d}",
                *[(f"a{i}", StepKind.TOOL, StepStatus.ERROR if i % 10 == 0 else StepStatus.OK)
                  for i in range(300)],
            )
        return d

    args = ["--limit", "1", *(["--explain"] if explain else [])]
    peaks = []
    was_tracing = tracemalloc.is_tracing()
    for d in (corpus(10), corpus(30)):
        runner.invoke(app, ["query", "error", str(d), *args])  # settle lazy imports and caches
        if not was_tracing:
            tracemalloc.start()
        try:
            tracemalloc.reset_peak()  # an enclosing session's earlier peak must not count
            baseline = tracemalloc.get_traced_memory()[0]
            res = runner.invoke(app, ["query", "error", str(d), *args])
            peaks.append(tracemalloc.get_traced_memory()[1] - baseline)
        finally:
            if not was_tracing:
                tracemalloc.stop()
        assert res.exit_code == 0, res.output
    growth = peaks[1] - peaks[0]
    assert growth < 2 * 2**20, f"peak grew {growth / 2**20:.1f} MiB for 20 more artifacts"


def _track_loads(monkeypatch):
    """Record, at every artifact load, which earlier traces and steps are still alive.

    Callers disable cyclic garbage collection for the run, so anything reported dead was freed
    by reference counting: a reference cycle only the collector would break shows up alive."""
    import weakref

    alive_at_load: list[list[str]] = []
    refs: list[tuple[str, weakref.ref, weakref.ref]] = []
    real = cli_mod._load_artifact_evidence

    def tracking(path):
        alive_at_load.append(
            [tid for tid, trace, step in refs if trace() is not None or step() is not None]
        )
        item = real(path)
        refs.append((item.trace.trace.trace_id, weakref.ref(item.trace),
                     weakref.ref(item.trace.steps[-1])))
        return item

    monkeypatch.setattr(cli_mod, "_load_artifact_evidence", tracking)
    return alive_at_load


@pytest.mark.parametrize("explain", [False, True])
def test_query_releases_each_trace_before_loading_the_next(tmp_path, monkeypatch, explain):
    # Peak memory stays flat only if the previous trace and its steps are gone *while* the next
    # one loads, not merely at the end of the pass. With --explain, the trace holding the one
    # kept match must stay alive; nothing else may.
    import gc

    d = tmp_path / "arts"
    d.mkdir()
    _failing_traces(d, "A", "B", "C", "D")
    alive_at_load = _track_loads(monkeypatch)
    gc.disable()
    try:
        args = ["query", "error", str(d), "--limit", "1", *(["--explain"] if explain else [])]
        res = runner.invoke(app, args)
    finally:
        gc.enable()
    assert res.exit_code == 0, res.output
    kept = ["A"] if explain else []
    assert alive_at_load == [[], kept, kept, kept]


def test_query_deferred_matching_failure_does_not_keep_its_trace(tmp_path, monkeypatch):
    # The first matching failure is held until every file has loaded; its traceback frames
    # must not keep that trace (or its match list) alive meanwhile.
    import gc

    d = tmp_path / "arts"
    d.mkdir()
    _failing_traces(d, "A", "B", "C")
    from tracegraph.analysis import patterns

    def boom(nt, pattern, *, index=None):
        raise _Injected(f"matcher broke on {nt.trace.trace_id}")

    monkeypatch.setattr(patterns, "iter_matches", boom)
    monkeypatch.setattr(cli_mod, "iter_matches", boom)
    alive_at_load = _track_loads(monkeypatch)
    gc.disable()
    try:
        res = runner.invoke(app, ["query", "error", str(d)])
    finally:
        gc.enable()
    assert isinstance(res.exception, _Injected)
    assert str(res.exception) == "matcher broke on A"
    assert alive_at_load == [[], [], []]


@pytest.mark.parametrize("command", ["query", "export-review-candidates"])
def test_ladybug_label_failure_stays_the_context_of_a_close_failure(
    tmp_path, monkeypatch, command
):
    # Labels are built while the per-trace store is open, so a close failure raised while a
    # label failure is propagating chains it as __context__, as before.
    store_cls = _ladybug_store()
    paths = _failing_traces(tmp_path, "A")

    def label(*args, **kwargs):
        raise _Injected("label broke")

    def close(self):
        raise OSError("close broke")

    monkeypatch.setattr(cli_mod, "match_for", label)
    monkeypatch.setattr(store_cls, "close", close)
    args = [command, "tool-failure", *map(str, paths), "--backend", "ladybug"]
    if command == "export-review-candidates":
        args += ["--out", str(tmp_path / "out.json")]
    res = runner.invoke(app, args)
    assert isinstance(res.exception, OSError), res.output
    assert str(res.exception) == "close broke"
    assert isinstance(res.exception.__context__, _Injected)


# --- --strict: a skipped directory file fails the batch (#50) -------------------------------


def _corrupt_beside_clean(tmp_path):
    """The #50 shape: the run holding the tool failure is truncated; the other run is clean."""
    d = tmp_path / "arts"
    d.mkdir()
    _failing_traces(d, "A")
    (d / "A.json").write_text((d / "A.json").read_text()[:40], encoding="utf-8")
    _write_linear_trace(d / "B.json", "B", ("plan", StepKind.CHAIN, StepStatus.OK))
    return d


def test_query_skipped_corrupt_artifact_passes_a_no_match_gate_without_strict(tmp_path):
    d = _corrupt_beside_clean(tmp_path)
    res = runner.invoke(app, ["query", "tool-failure", str(d)])
    assert res.exit_code == 1  # the leniency #50 is about, unchanged by default
    assert "A.json" in _panel_text(res.stderr)


@pytest.mark.parametrize("backend", ["memory", "ladybug"])
def test_query_strict_fails_on_a_skipped_directory_file(tmp_path, backend):
    if backend == "ladybug":
        _ladybug_store()
    d = _corrupt_beside_clean(tmp_path)
    res = runner.invoke(app, ["query", "tool-failure", str(d), "--strict", "--backend", backend])
    assert res.exit_code == 2
    err = _panel_text(res.stderr)
    assert err.index("skipped 1 non-artifact file(s): A.json") < err.index("--strict: 1 discovered")
    assert "no matches" not in res.stdout and "match(es)" not in res.stdout


def test_query_strict_with_only_strays_reports_strict_not_no_valid_artifacts(tmp_path):
    d = tmp_path / "arts"
    d.mkdir()
    (d / "stray.json").write_text("{}", encoding="utf-8")
    res = runner.invoke(app, ["query", "error", str(d), "--strict"])
    assert res.exit_code == 2
    err = _panel_text(res.stderr)
    assert "--strict: 1 discovered" in err and "no valid artifacts" not in err


def test_query_strict_yields_to_an_explicit_load_error_and_precedes_duplicates(tmp_path):
    d = tmp_path / "arts"
    d.mkdir()
    a, = _failing_traces(d, "A")
    (d / "stray.json").write_text("{}", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = runner.invoke(app, ["query", "error", str(d), str(bad), "--strict"])
    assert res.exit_code == 2
    assert "cannot load artifact" in _panel_text(res.stderr)
    dup = tmp_path / "again.json"
    dup.write_bytes(a.read_bytes())
    res = runner.invoke(app, ["query", "error", str(d), str(dup), "--strict"])
    assert res.exit_code == 2
    err = _panel_text(res.stderr)
    assert "--strict: 1 discovered" in err and "duplicate trace_id" not in err


@pytest.mark.parametrize("extra", [[], ["--limit", "1"], ["--explain"]])
def test_query_strict_without_strays_changes_nothing(tmp_path, extra):
    d = tmp_path / "arts"
    d.mkdir()
    _failing_traces(d, "A", "B")
    plain = runner.invoke(app, ["query", "error", str(d), *extra])
    strict = runner.invoke(app, ["query", "error", str(d), *extra, "--strict"])
    assert (strict.exit_code, strict.stdout, strict.stderr) == (
        plain.exit_code, plain.stdout, plain.stderr
    )


def test_export_review_candidates_strict_fails_and_leaves_the_output_alone(tmp_path):
    d = _corrupt_beside_clean(tmp_path)
    out = tmp_path / "candidates.json"
    res = runner.invoke(app, ["export-review-candidates", "tool-failure", str(d), "-o", str(out)])
    assert res.exit_code == 0 and out.exists()  # default: exported with a skip warning
    before = out.read_bytes()
    res = runner.invoke(
        app, ["export-review-candidates", "tool-failure", str(d), "-o", str(out), "--strict"]
    )
    assert res.exit_code == 2
    assert "--strict: 1 discovered" in _panel_text(res.stderr)
    assert out.read_bytes() == before


def test_export_review_candidates_strict_without_strays_writes_the_same_report(tmp_path):
    d = tmp_path / "arts"
    d.mkdir()
    _write_linear_trace(d / "B.json", "B", ("plan", StepKind.CHAIN, StepStatus.OK))
    plain, strict = tmp_path / "plain.json", tmp_path / "strict.json"
    runner.invoke(app, ["export-review-candidates", "tool-failure", str(d), "-o", str(plain)])
    res = runner.invoke(
        app, ["export-review-candidates", "tool-failure", str(d), "-o", str(strict), "--strict"]
    )
    assert res.exit_code == 0, res.output
    assert strict.read_bytes() == plain.read_bytes()


def test_query_strict_refusal_precedes_a_deferred_matching_failure(tmp_path, monkeypatch):
    d = tmp_path / "arts"
    d.mkdir()
    _failing_traces(d, "A")
    (d / "stray.json").write_text("{}", encoding="utf-8")
    _break_memory_matcher(monkeypatch)
    res = runner.invoke(app, ["query", "error", str(d), "--strict"])
    assert res.exit_code == 2
    assert "--strict: 1 discovered" in _panel_text(res.stderr)


def test_export_review_candidates_strict_refusal_never_calls_the_writer(tmp_path, monkeypatch):
    d = _corrupt_beside_clean(tmp_path)
    out = tmp_path / "absent" / "candidates.json"

    def no_write(*args, **kwargs):
        raise AssertionError("the report writer ran on a strict refusal")

    monkeypatch.setattr(cli_mod, "save_candidate_report", no_write)
    res = runner.invoke(
        app, ["export-review-candidates", "tool-failure", str(d), "-o", str(out), "--strict"]
    )
    assert res.exit_code == 2, res.output
    assert "could not be loaded as tracegraph artifacts" in _panel_text(res.stderr)
    assert not out.exists() and not out.parent.exists()
