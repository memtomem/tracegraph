"""CLI smoke test: ingest a real SqliteSaver DB, then inspect / explain / diff."""

import builtins
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
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

runner = CliRunner()


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


def test_kuzu_backend_missing_optional_dependency_reports_bad_parameter(monkeypatch):
    monkeypatch.delitem(sys.modules, "tracegraph.store.kuzu", raising=False)
    monkeypatch.delitem(sys.modules, "kuzu", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "kuzu":
            raise ModuleNotFoundError("No module named 'kuzu'", name="kuzu")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(typer.BadParameter, match=r"tracegraph\[cypher\]"):
        cli_mod._store_cls(cli_mod.QueryBackend.KUZU)


def test_kuzu_backend_broken_import_is_not_hidden(monkeypatch):
    monkeypatch.delitem(sys.modules, "tracegraph.store.kuzu", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tracegraph.store.kuzu":
            raise ImportError("backend broken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match="backend broken"):
        cli_mod._store_cls(cli_mod.QueryBackend.KUZU)


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
