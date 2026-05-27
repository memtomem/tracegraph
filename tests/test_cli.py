"""CLI smoke test: ingest a real SqliteSaver DB, then inspect / explain / diff."""

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from tiny_agent import run
from typer.testing import CliRunner

from tracegraph.cli import app

runner = CliRunner()


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
