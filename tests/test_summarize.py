"""`tracegraph summarize`: preset matches grouped by a versioned, body-free signature."""

import gc
import hashlib
import json
import tracemalloc
import weakref

import pytest
from typer.testing import CliRunner

from tracegraph import artifact, cli
from tracegraph.analysis import motifs
from tracegraph.analysis.patterns import PRESETS
from tracegraph.cli import app
from tracegraph.model import (
    Capture,
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

runner = CliRunner()
CHAIN, TOOL, AGENT = StepKind.CHAIN, StepKind.TOOL, StepKind.AGENT
OK, ERROR, UNSET = StepStatus.OK, StepStatus.ERROR, StepStatus.UNSET


def _trace(trace_id, specs, *, origins=None, source_kind="x", fidelity=None, links=None,
           capture=None, edges=None):
    """A linear trace of ``specs`` = ``(name, kind, status[, unknown_fields])``, or ``edges``.

    ``origins`` gives each linear edge's origin. Every step is caused by the one before unless
    ``edges`` (a list of ``(effect_index, cause_index)``) says otherwise.
    """
    steps = []
    for i, spec in enumerate(specs):
        name, kind, status, *rest = spec
        steps.append(Step(step_id=f"{trace_id}-{i}", trace_id=trace_id, seq=i, name=name,
                          kind=kind, status=status, unknown_fields=rest[0] if rest else []))
    pairs = edges if edges is not None else [(i, i - 1) for i in range(1, len(specs))]
    causal = [
        Edge(type=EdgeType.CAUSED_BY, src=f"{trace_id}-{effect}", dst=f"{trace_id}-{cause}",
             origin=(origins[n] if origins else None))
        for n, (effect, cause) in enumerate(pairs)
    ]
    header = {"trace_id": trace_id, "source_kind": source_kind, "links_preserved": links,
              "capture": capture}
    if fidelity is not None:
        header["causal_fidelity"] = fidelity
    return normalize(RawTrace(trace=Trace(**header), steps=steps, causal_edges=causal))


def _save(tmp_path, nt, name=None):
    path = tmp_path / f"{name or nt.trace.trace_id}.json"
    artifact.save(nt, path)
    return path


def _retry(trace_id, *, origin=EdgeOrigin.GRAPH_PARENT, **kw):
    return _trace(trace_id, [("search", TOOL, OK), ("retry:search", CHAIN, OK),
                             ("search", TOOL, ERROR)], origins=[origin, origin], **kw)


def _summary(tmp_path, preset, *paths, extra=(), name="summary"):
    out = tmp_path / "out" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    res = runner.invoke(app, ["summarize", preset, *map(str, paths), "--out", str(out), *extra])
    report = json.loads(out.read_text()) if out.exists() else None
    return res, report


def test_id_renamed_equivalent_motifs_share_one_group(tmp_path):
    a = _save(tmp_path, _retry("A"))
    b = _save(tmp_path, _retry("B-other-ids"))
    res, report = _summary(tmp_path, "tool-retry-failure", a, b)
    assert res.exit_code == 0, res.output
    [group] = report["groups"]
    assert (group["match_count"], group["trace_count"]) == (2, 2)
    assert [s["name"] for s in group["signature"]["steps"]] == ["search", "retry:search", "search"]
    assert group["signature"]["edge_origins"] == ["graph_parent", "graph_parent"]
    assert "A-0" not in json.dumps(group["signature"])  # no ids in the signature


def test_different_edge_origins_are_different_groups(tmp_path):
    a = _save(tmp_path, _retry("A", origin=EdgeOrigin.GRAPH_PARENT))
    b = _save(tmp_path, _retry("B", origin=EdgeOrigin.SPAN_LINK))
    _, report = _summary(tmp_path, "tool-retry-failure", a, b)
    assert sorted(g["signature"]["edge_origins"][0] for g in report["groups"]) == [
        "graph_parent", "span_link"]
    assert all(g["trace_count"] == 1 for g in report["groups"])


def test_repeated_matches_in_one_trace_count_matches_not_traces(tmp_path):
    nt = _trace("R", [("plan", CHAIN, OK)] + [("search", TOOL, ERROR)] * 3,
                edges=[(1, 0), (2, 0), (3, 0)])
    _, report = _summary(tmp_path, "tool-failure", _save(tmp_path, nt))
    [group] = report["groups"]
    assert (group["match_count"], group["trace_count"]) == (3, 1)
    assert report["coverage"]["traces_matched"] == 1


def test_the_same_artifact_twice_is_a_duplicate_trace(tmp_path):
    a = _save(tmp_path, _retry("A"))
    res, report = _summary(tmp_path, "tool-retry-failure", a, tmp_path)
    assert res.exit_code == 2
    assert "duplicate trace_id" in res.output and report is None


@pytest.mark.parametrize("header", [{"source_kind": "otlp"},
                                    {"fidelity": CausalFidelity.PARENT_ONLY}])
def test_incompatible_source_or_fidelity_is_a_separate_group(tmp_path, header):
    a = _save(tmp_path, _retry("A"))
    b = _save(tmp_path, _retry("B", **header))
    _, report = _summary(tmp_path, "tool-retry-failure", a, b)
    assert len(report["groups"]) == 2


def test_a_corrupt_artifact_is_skipped_and_counted_or_refused_with_strict(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _save(corpus, _retry("A"))
    (corpus / "broken.json").write_text("{not json")
    res, report = _summary(tmp_path, "tool-retry-failure", corpus)
    assert res.exit_code == 0
    cov = report["coverage"]
    assert (cov["artifacts_discovered"], cov["artifacts_valid"], cov["artifacts_skipped"]) == (2, 1, 1)
    assert "broken.json" in res.output

    out = tmp_path / "out" / "summary.json"
    before = out.read_bytes()
    res, _ = _summary(tmp_path, "tool-retry-failure", corpus, extra=["--strict"])
    assert res.exit_code == 2
    assert out.read_bytes() == before  # a refused run leaves the previous report alone


def test_a_non_identifier_name_is_redacted_in_the_signature(tmp_path):
    nt = _trace("P", [("call the search API", TOOL, ERROR)])
    _, report = _summary(tmp_path, "tool-failure", _save(tmp_path, nt))
    [step] = report["groups"][0]["signature"]["steps"]
    assert step["name"].startswith("redacted:") and "search" not in step["name"]


def test_representatives_are_the_lowest_candidates_whatever_the_input_order(tmp_path):
    paths = [_save(tmp_path, _retry(f"T{i}")) for i in range(5)]
    res_f, forward = _summary(tmp_path, "tool-retry-failure", *paths, name="forward")
    res_b, backward = _summary(tmp_path, "tool-retry-failure", *reversed(paths), name="backward")
    assert (res_f.exit_code, res_b.exit_code) == (0, 0)
    assert forward == backward
    candidates = sorted(
        ("sha256:" + hashlib.sha256(p.read_bytes()).hexdigest(),
         [f"{p.stem}-0", f"{p.stem}-1", f"{p.stem}-2"])
        for p in paths
    )
    [group] = forward["groups"]
    assert [(r["artifact_digest"], r["step_ids"]) for r in group["representatives"]] == (
        candidates[:motifs.MAX_REPRESENTATIVES])


def test_no_match_exits_1_and_still_writes_the_coverage(tmp_path):
    clean = _trace("C", [("plan", CHAIN, OK), ("search", TOOL, OK)])
    res, report = _summary(tmp_path, "tool-failure", _save(tmp_path, clean))
    assert res.exit_code == 1
    assert report["groups"] == [] and report["coverage"]["artifacts_valid"] == 1
    assert "no matches in 1 valid artifact(s)" in res.output
    assert "root cause" not in res.output  # no claim either way without a group


def test_a_gap_preset_is_refused_and_leaves_the_output_alone(tmp_path):
    out = tmp_path / "out" / "summary.json"
    out.parent.mkdir()
    out.write_text("previous")
    res = runner.invoke(app, ["summarize", "repeated-agent-failure",
                              str(_save(tmp_path, _retry("A"))), "--out", str(out)])
    assert res.exit_code == 2
    assert "not summarizable" in res.output
    assert out.read_text() == "previous"
    assert [p for p, preset in PRESETS.items() if not motifs.is_summarizable(preset)] == [
        "repeated-agent-failure", "tool-repeat-failure-heuristic", "tool-retry-failure-near"]


def test_out_must_not_be_an_input_and_a_report_in_a_scanned_directory_is_excluded(tmp_path):
    a = _save(tmp_path, _retry("A"))
    res = runner.invoke(app, ["summarize", "tool-retry-failure", str(a), "--out", str(a)])
    assert res.exit_code == 2 and "must not overwrite an input" in res.output

    out = tmp_path / "summary.json"  # inside the scanned directory
    for _ in range(2):
        res = runner.invoke(app, ["summarize", "tool-retry-failure", str(tmp_path),
                                  "--out", str(out)])
        assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["coverage"]["artifacts_discovered"] == 1


def test_coverage_counts_only_unknowns_that_can_hide_this_preset(tmp_path):
    unset_tool = _trace("U", [("plan", CHAIN, OK), ("search", TOOL, UNSET)],
                        capture=Capture(gaps=[]))
    irrelevant = _trace("I", [("search", TOOL, ERROR)],
                        capture=Capture.from_counts({"otlp.unreadable_total_cost": 1}))
    no_record = _trace("N", [("plan", CHAIN, OK)])
    _, report = _summary(tmp_path, "tool-failure",
                         *(_save(tmp_path, nt) for nt in (unset_tool, irrelevant, no_record)))
    cov = report["coverage"]
    assert cov["traces_may_hide_matches"] == 1  # the unset TOOL only
    assert cov["traces_with_disclosed_gaps"] == 1  # the cost gap, counted literally
    assert cov["traces_without_provenance"] == 1


def test_tied_groups_sort_across_every_links_preserved_value(tmp_path):
    paths = [_save(tmp_path, _retry(f"L{n}", links=links))
             for n, links in enumerate([None, False, True])]
    res_f, forward = _summary(tmp_path, "tool-retry-failure", *paths, name="forward")
    res_b, backward = _summary(tmp_path, "tool-retry-failure", *reversed(paths), name="backward")
    assert (res_f.exit_code, res_b.exit_code) == (0, 0)
    assert forward == backward
    assert len(forward["groups"]) == 3


def test_a_defaulted_kind_does_not_confirm_a_match(tmp_path):
    capture = Capture(gaps=[])
    defaulted = _trace("D", [("search", TOOL, OK), ("retry:search", CHAIN, OK, ["kind"]),
                             ("search", TOOL, ERROR)], capture=capture)
    declared = _trace("K", [("search", TOOL, OK), ("retry:search", CHAIN, OK),
                            ("search", TOOL, ERROR)], capture=capture)
    _, report = _summary(tmp_path, "tool-retry-failure",
                         _save(tmp_path, defaulted), _save(tmp_path, declared))
    [group] = report["groups"]
    assert group["representatives"][0]["step_ids"][0].startswith("K-")
    cov = report["coverage"]
    assert (cov["unconfirmed_matches"], cov["traces_with_unconfirmed_matches"]) == (1, 1)


def test_a_missing_name_never_merges_with_the_recorded_name_unknown(tmp_path):
    missing = _trace("M", [(None, TOOL, ERROR)])
    literal = _trace("L", [("unknown", TOOL, ERROR)])
    _, report = _summary(tmp_path, "tool-failure",
                         _save(tmp_path, missing), _save(tmp_path, literal))
    shapes = sorted(((s["name"], s["unknown"]) for g in report["groups"]
                     for s in g["signature"]["steps"]), key=repr)
    assert shapes == [("unknown", []), (None, ["name"])]


def _fan(width):
    """``width`` tools -> one retry marker -> ``width`` failing tools: width² retry matches."""
    specs = ([("search", TOOL, OK)] * width + [("retry:search", CHAIN, OK)]
             + [("search", TOOL, ERROR)] * width)
    marker = width
    edges = [(marker, i) for i in range(width)] + [(marker + 1 + i, marker) for i in range(width)]
    return _trace(f"F{width}", specs, edges=edges)


def _peak_while_adding(nt):
    aggregator = motifs.MotifAggregator(PRESETS["tool-retry-failure"])
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        aggregator.add(nt, "sha256:" + "0" * 64)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()
    return aggregator, peak


def test_memory_does_not_grow_with_the_match_count():
    small, peak_small = _peak_while_adding(_fan(40))
    large, peak_large = _peak_while_adding(_fan(160))
    [group] = large.summary(discovered=1, valid=1, skipped=0).groups
    assert group.match_count == 160 * 160  # every match is counted ...
    assert len(group.representatives) == motifs.MAX_REPRESENTATIVES  # ... few are kept
    # 16x the matches (1600 -> 25600); collecting them would cost megabytes. The trace and the
    # matcher's per-trace state grow with the step count only.
    assert peak_large - peak_small < 1 << 19, (peak_small, peak_large)  # measured ~26 KiB vs ~2 MiB


def test_the_report_round_trips_through_its_strict_model(tmp_path):
    _, report = _summary(tmp_path, "tool-retry-failure", _save(tmp_path, _retry("A")))
    model = motifs.MotifSummary.model_validate(report)
    assert json.loads(motifs.dumps(model)) == report
    with pytest.raises(ValueError):
        motifs.MotifSummary.model_validate({**report, "extra": 1})


def test_the_rendered_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.console, "width", 200)
    a = _save(tmp_path, _retry("A"))
    digest = "sha256:" + hashlib.sha256(a.read_bytes()).hexdigest()
    res = runner.invoke(app, ["summarize", "tool-retry-failure", str(a)])
    assert res.exit_code == 0, res.output
    assert res.output.splitlines() == [
        "tool-retry-failure@v2: {kind=TOOL} → {name^=retry:, kind=CHAIN} → {kind=TOOL, "
        "status=error, name=#0}  (over 1 trace(s))",
        "",
        "1. 1 trace(s), 1 match(es): search [TOOL, loop] (ok) →[graph_parent]→ retry:search "
        "[CHAIN, loop] (ok) →[graph_parent]→ search [TOOL, loop] (error)",
        "   source x, fidelity legacy_unknown, links None, heuristic edges not recorded",
        f"   e.g. {digest} steps A-0, A-1, A-2",
        "",
        "1 valid of 1 discovered (0 skipped); 1 trace(s) matched. May hide matches: 0 trace(s); "
        "disclosed capture gaps: 0; no capture record: 1; unconfirmed matches (not grouped): 0 in "
        "0 trace(s).",
        "A group is an observed preset shape; it does not show a shared root cause.",
    ]


def test_producer_strings_print_literally(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.console, "width", 200)
    nt = _trace(":ok:", [(":ok:", TOOL, ERROR)], source_kind=":ok:")
    res = runner.invoke(app, ["summarize", "tool-failure", str(_save(tmp_path, nt, "e"))],
                        terminal_width=200)
    assert "🆗" not in res.output
    assert ":ok: [TOOL, loop] (error)" in res.output and "source :ok:" in res.output
    assert "steps :ok:-0" in res.output


def test_an_input_error_wins_over_a_failure_while_grouping(tmp_path, monkeypatch):
    first = _save(tmp_path, _retry("A"))
    later = tmp_path / "later.json"
    later.write_text("{not json")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "stray.json").write_text("{}")

    def boom(self, nt, digest):
        raise RuntimeError("grouping failed")

    monkeypatch.setattr(motifs.MotifAggregator, "add", boom)
    res = runner.invoke(app, ["summarize", "tool-retry-failure", str(first), str(corpus),
                              str(later)])
    assert res.exit_code == 2, res.output  # the explicit bad file, as for `query`
    assert "later.json" in res.output
    assert "stray.json" in res.output  # skip warnings still flushed

    res = runner.invoke(app, ["summarize", "tool-retry-failure", str(first)])
    assert isinstance(res.exception, RuntimeError)  # with no input error, the failure surfaces


def _track_loads(monkeypatch):
    """At every artifact load, which earlier traces are still alive (gc disabled by callers)."""
    alive_at_load: list[list[str]] = []
    refs: list[tuple[str, weakref.ref]] = []
    real = cli._load_artifact_evidence

    def tracking(path):
        alive_at_load.append([tid for tid, ref in refs if ref() is not None])
        item = real(path)
        refs.append((item.trace.trace.trace_id, weakref.ref(item.trace)))
        return item

    monkeypatch.setattr(cli, "_load_artifact_evidence", tracking)
    return alive_at_load


@pytest.mark.parametrize("shape", ["plain", "duplicate", "failing"])
def test_each_trace_is_released_before_the_next_loads(tmp_path, monkeypatch, shape):
    a = _save(tmp_path, _retry("A"))
    second = _save(tmp_path, _retry("A"), "A-again") if shape == "duplicate" else _save(
        tmp_path, _retry("B"))
    c = _save(tmp_path, _retry("C"))
    if shape == "failing":
        def boom(self, nt, digest):
            raise RuntimeError("grouping failed")

        monkeypatch.setattr(motifs.MotifAggregator, "add", boom)
    alive_at_load = _track_loads(monkeypatch)
    gc.disable()
    try:
        res = runner.invoke(app, ["summarize", "tool-retry-failure", str(a), str(second), str(c)])
    finally:
        gc.enable()
    assert res.exit_code == {"plain": 0, "duplicate": 2, "failing": 1}[shape], res.output
    assert alive_at_load == [[], [], []]


def test_heuristically_chosen_edges_are_a_separate_group(tmp_path):
    # OTLP records a parent picked by timing with the same origin and fidelity as an unambiguous
    # one; only the trace's capture gap tells them apart.
    def plan(trace_id, capture):
        return _trace(trace_id, [("plan", CHAIN, OK), ("search", TOOL, ERROR)],
                      origins=[EdgeOrigin.GRAPH_PARENT], source_kind="otlp",
                      fidelity=CausalFidelity.DECLARED_DAG, capture=capture)

    declared = plan("D", Capture(gaps=[]))
    by_timing = plan("T", Capture.from_counts({"otlp.graph_parent_by_timing": 1}))
    unrecorded = plan("N", None)
    paths = [_save(tmp_path, nt) for nt in (declared, by_timing, unrecorded)]
    _, report = _summary(tmp_path, "plan-then-tool-failure", *paths)
    assert sorted(repr(g["signature"]["heuristic_edges"]) for g in report["groups"]) == [
        "False", "None", "True"]

    # A single-step shape has no edge, so the disclosure does not split it.
    _, single = _summary(tmp_path, "tool-failure", *paths, name="single")
    [group] = single["groups"]
    assert group["signature"]["heuristic_edges"] is False and group["trace_count"] == 3


def test_groups_that_differ_only_in_kind_render_differently(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.console, "width", 200)
    agent = _save(tmp_path, _trace("G", [("worker", AGENT, ERROR)]))
    tool = _save(tmp_path, _trace("W", [("worker", TOOL, ERROR)]))
    res = runner.invoke(app, ["summarize", "error", str(agent), str(tool)])
    assert res.exit_code == 0, res.output
    assert "worker [AGENT, loop] (error)" in res.output
    assert "worker [TOOL, loop] (error)" in res.output
