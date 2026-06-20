"""CLI smoke test: ingest a real SqliteSaver DB, then inspect / explain / diff."""

import builtins
import re
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
