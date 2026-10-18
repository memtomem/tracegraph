"""`tracegraph check`: PASS, FAIL or INSUFFICIENT_EVIDENCE over an explicit pair and explicit rules.

The cases below are the counterexamples that shaped the rules: each one is a pair where a
simpler rule would have reached PASS or FAIL without the evidence to support it.
"""

from __future__ import annotations

import json
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from jsonschema import Draft202012Validator, ValidationError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter, OTLPSpanAdapter
from tracegraph.analysis import check
from tracegraph.analysis.diagnose import analyze
from tracegraph.cli import app
from tracegraph.model import (
    Capture,
    CaptureGap,
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    ScheduleGraph,
    Step,
    StepKind,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

SCHEMA = json.loads((Path(__file__).parents[1] / "contracts" / "check-report.schema.json").read_text())
runner = CliRunner()

FAILURES = "failures-not-increased"
TOPOLOGY = "topology-unchanged"
SHAPE = "topology-unchanged:shape"


def pattern(preset: str) -> str:
    return f"pattern-not-increased:{preset}"


def _trace(
    tid: str,
    steps: list[tuple],
    edges: list[tuple[str, str]] = (),
    *,
    source: str = "otlp",
    gaps: dict[str, int] | None = None,
    capture: bool = True,
    links: bool | None = True,
) -> NormalizedTrace:
    """Steps as ``(id, kind, name, status[, unknown_fields[, error_msg]])``; edges effect -> cause."""
    built = []
    for seq, (sid, kind, name, status, *rest) in enumerate(steps):
        unknown = rest[0] if rest else []
        message = rest[1] if len(rest) > 1 else ("boom" if status is StepStatus.ERROR else None)
        built.append(Step(step_id=sid, trace_id=tid, seq=seq, kind=kind, name=name, status=status,
                          error_msg=message, unknown_fields=unknown))
    header = Trace(
        trace_id=tid,
        source_kind=source,
        causal_fidelity=CausalFidelity.DECLARED_DAG,
        links_preserved=links,
        capture=(Capture(gaps=[CaptureGap(code=c, count=n) for c, n in sorted((gaps or {}).items())])
                 if capture else None),
    )
    return normalize(RawTrace(
        trace=header,
        steps=built,
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src=a, dst=b, origin=EdgeOrigin.GRAPH_PARENT)
                      for a, b in edges],
    ))


OK, ERR, UNSET = StepStatus.OK, StepStatus.ERROR, StepStatus.UNSET
AGENT, TOOL, CHAIN = StepKind.AGENT, StepKind.TOOL, StepKind.CHAIN


def _run(current, baseline, *rules):
    report = check.evaluate(current, baseline, check.parse_rules(list(rules)))
    Draft202012Validator(SCHEMA).validate(json.loads(check.dumps(report)))
    return report


def _outcomes(report) -> dict[str, str]:
    return {r.rule_id + ("" if not r.params else ":" + ",".join(f"{v}" for _, v in sorted(r.params.items()))): r.outcome
            for r in report.rules}


def _one(report) -> str:
    [rule] = report.rules
    return rule.outcome


# --- the issue's case and plain outcomes --------------------------------------------------------


def _pair(after: StepStatus):
    base = _trace("b", [("a", AGENT, "agent", OK), ("t", TOOL, "search", OK)], [("t", "a")])
    cur = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", after)], [("t", "a")])
    return cur, base


def test_a_status_only_regression_fails():
    cur, base = _pair(ERR)
    report = _run(cur, base, FAILURES, pattern("tool-failure"), TOPOLOGY)
    assert report.verdict == "FAIL"
    assert _outcomes(report) == {FAILURES: "FAIL", "pattern-not-increased:1,tool-failure": "FAIL",
                                 TOPOLOGY + ":labels": "PASS"}
    [evidence] = report.rules[0].evidence
    assert (evidence.signature.kind, evidence.signature.name, evidence.current_step_ids) == ("TOOL", "search", ["t"])


def test_unchanged_evidence_passes_every_rule():
    cur, _ = _pair(OK)
    report = _run(cur, cur, FAILURES, pattern("tool-failure"), pattern("tool-retry-failure"), TOPOLOGY, SHAPE)
    assert report.verdict == "PASS"
    assert set(_outcomes(report).values()) == {"PASS"}


def test_equal_unknowns_are_still_unknown():
    cur = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", UNSET)], [("t", "a")])
    assert _one(_run(cur, cur, FAILURES)) == "INSUFFICIENT_EVIDENCE"


def test_an_expected_structural_change_fails_only_the_structural_rule():
    _, base = _pair(OK)
    cur = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", OK), ("u", TOOL, "fetch", OK)],
                 [("t", "a"), ("u", "a")])
    assert _outcomes(_run(cur, base, FAILURES, TOPOLOGY)) == {FAILURES: "PASS", TOPOLOGY + ":labels": "FAIL"}
    assert _run(cur, base, FAILURES).verdict == "PASS"


def test_a_fail_outranks_insufficient_evidence():
    cur, base = _pair(ERR)
    cur_unset = _trace("c", [("a", AGENT, "agent", UNSET), ("t", TOOL, "search", ERR)], [("t", "a")])
    report = _run(cur_unset, base, FAILURES, pattern("gate-failure-after-success"))
    assert _outcomes(report)[FAILURES] == "FAIL"
    assert report.verdict == "FAIL"


# --- identity across runs ---------------------------------------------------------------------


def test_counts_cannot_see_two_same_named_steps_swap_failures():
    # Documented limitation: which of two `search` calls failed is not recorded across runs.
    base = _trace("b", [("a", AGENT, "agent", OK), ("s1", TOOL, "search", ERR), ("s2", TOOL, "search", OK)],
                  [("s1", "a"), ("s2", "a")])
    cur = _trace("c", [("a", AGENT, "agent", OK), ("s2", TOOL, "search", ERR)], [("s2", "a")])
    assert _one(_run(cur, base, FAILURES)) == "PASS"


def test_a_wrapper_above_an_unchanged_failure_is_not_a_new_failure():
    base = _trace("b", [("a", AGENT, "agent", OK), ("t", TOOL, "search", ERR)], [("t", "a")])
    cur = _trace("c", [("a", AGENT, "agent", OK), ("w", CHAIN, "wrap", OK), ("t", TOOL, "search", ERR)],
                 [("w", "a"), ("t", "w")])
    assert _outcomes(_run(cur, base, FAILURES, pattern("tool-failure"), TOPOLOGY)) == {
        FAILURES: "PASS", "pattern-not-increased:1,tool-failure": "PASS", TOPOLOGY + ":labels": "FAIL"}


def test_a_failure_replaced_by_a_different_one_fails_although_the_count_is_equal():
    base = _trace("b", [("a", AGENT, "agent", OK), ("t", TOOL, "search", ERR)], [("t", "a")])
    cur = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "fetch", ERR)], [("t", "a")])
    assert _outcomes(_run(cur, base, FAILURES, pattern("tool-failure"))) == {
        FAILURES: "FAIL", "pattern-not-increased:1,tool-failure": "FAIL"}


# --- unknown evidence -------------------------------------------------------------------------


def test_failures_of_unknown_name_cannot_decide_against_named_ones():
    unnamed = _trace("b", [("x", AGENT, None, ERR, ["kind", "name"]), ("y", AGENT, None, ERR, ["kind", "name"])],
                     [("y", "x")])
    named = _trace("c", [("x", AGENT, "planner", ERR), ("y", AGENT, "planner", ERR)], [("y", "x")])
    report = _run(named, unnamed, FAILURES, pattern("repeated-agent-failure"))
    assert set(_outcomes(report).values()) == {"INSUFFICIENT_EVIDENCE"}
    report = _run(unnamed, named, FAILURES, pattern("repeated-agent-failure"))
    assert set(_outcomes(report).values()) == {"INSUFFICIENT_EVIDENCE"}


def test_more_failures_fail_even_when_their_names_are_unknown():
    # LangGraph error-channel failures carry no reliable name; the total still rose for sure.
    base = _trace("b", [("x", CHAIN, None, OK, ["kind", "name"])], source="langgraph", links=None)
    cur = _trace("c", [("x", CHAIN, None, ERR, ["kind", "name"])], source="langgraph", links=None)
    report = _run(cur, base, FAILURES)
    assert _one(report) == "FAIL"
    [evidence] = report.rules[0].evidence
    assert evidence.signature is None and evidence.current_step_ids == ["x"]


def test_a_withheld_message_cannot_rule_out_a_timeout():
    known = _trace("b", [("t", TOOL, "search", ERR, [], "connection reset")])
    withheld = _trace("c", [("t", TOOL, "search", ERR, ["error_msg"], "error")])
    assert _one(_run(withheld, known, pattern("timeout"))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(known, withheld, pattern("timeout"))) == "PASS"
    # Messages are irrelevant to presets that do not read them.
    assert _one(_run(withheld, known, pattern("tool-failure"))) == "PASS"


def test_an_unset_agent_does_not_make_a_gate_failure_after_success():
    def pair(agent_status):
        return _trace("t", [("a", AGENT, "worker", agent_status), ("g", TOOL, "gate:review", ERR)], [("g", "a")])
    assert _one(_run(pair(UNSET), pair(ERR), pattern("gate-failure-after-success"))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(pair(OK), pair(UNSET), pattern("gate-failure-after-success"))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(pair(OK), pair(ERR), pattern("gate-failure-after-success"))) == "FAIL"


def test_unknowns_a_preset_cannot_match_do_not_block_it():
    base = _trace("b", [("a", AGENT, "agent", OK), ("c", CHAIN, "think", OK), ("b", AGENT, "agent", OK)],
                  [("c", "a"), ("b", "c")])
    cur = _trace("c", [("a", AGENT, "agent", OK), ("c", CHAIN, "think", UNSET), ("b", AGENT, "agent", OK)],
                 [("c", "a"), ("b", "c")])
    # An unset CHAIN can never become a failing TOOL, nor an AGENT at either end of the gap.
    assert _one(_run(cur, base, pattern("tool-failure"))) == "PASS"
    assert _one(_run(cur, base, pattern("repeated-agent-failure"))) == "PASS"
    assert _one(_run(cur, base, FAILURES)) == "INSUFFICIENT_EVIDENCE"


def _retry_doc(marker_kind):
    def span(sid, name, kind, status, parent=None):
        attrs = [] if kind is None else [{"key": "openinference.span.kind", "value": {"stringValue": kind}}]
        out = {"traceId": "t", "spanId": sid, "name": name, "status": {"code": status}, "attributes": attrs}
        if parent:
            out["parentSpanId"] = parent
        return out
    return {"resourceSpans": [{"scopeSpans": [{"spans": [
        span("first", "demo::tool", "TOOL", 1),
        span("retry", "retry:demo::tool", marker_kind, 1, "first"),
        span("last", "demo::tool", "TOOL", 2, "retry"),
    ]}]}]}


def _otlp(doc) -> NormalizedTrace:
    return normalize(OTLPSpanAdapter(doc).ingest("t"))


def test_a_defaulted_kind_cannot_complete_a_retry_match():
    # Without a kind attribute the marker defaults to CHAIN, which the preset requires.
    defaulted, declared = _otlp(_retry_doc(None)), _otlp(_retry_doc("LLM"))
    assert _one(_run(defaulted, declared, pattern("tool-retry-failure"))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(_otlp(_retry_doc("CHAIN")), declared, pattern("tool-retry-failure"))) == "FAIL"


# --- comparability ----------------------------------------------------------------------------


def test_heuristic_edges_block_only_graph_dependent_rules():
    from test_otlp_adapter import _loop_doc

    timed = normalize(OTLPSpanAdapter(_loop_doc(1_000_000_000)).ingest("t"))
    outcomes = _outcomes(_run(timed, timed, FAILURES, pattern("tool-failure"), pattern("plan-then-tool-failure"), SHAPE))
    assert outcomes == {FAILURES: "PASS", "pattern-not-increased:1,tool-failure": "PASS",
                        "pattern-not-increased:1,plan-then-tool-failure": "INSUFFICIENT_EVIDENCE",
                        TOPOLOGY + ":shape": "INSUFFICIENT_EVIDENCE"}


def test_links_on_and_off_are_not_comparable_structurally():
    cur, base = _pair(OK)
    off = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", OK)], [("t", "a")], links=False)
    assert _one(_run(off, base, SHAPE)) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(off, base, pattern("plan-then-tool-failure"))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(off, base, pattern("tool-failure"))) == "PASS"


def test_different_sources_are_not_comparable():
    cur, base = _pair(OK)
    phoenix = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", OK)], [("t", "a")], source="phoenix_cli")
    report = _run(phoenix, base, FAILURES, SHAPE)
    assert set(_outcomes(report).values()) == {"INSUFFICIENT_EVIDENCE"}
    assert "different sources" in report.rules[0].reasons[0]


def test_artifacts_without_capture_provenance_are_insufficient():
    cur, base = _pair(ERR)
    legacy = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", ERR)], [("t", "a")], capture=False)
    for pair in ((legacy, base), (cur, _trace("b", [("a", AGENT, "agent", OK)], capture=False))):
        report = _run(*pair, FAILURES, SHAPE)
        assert report.verdict == "INSUFFICIENT_EVIDENCE"
        assert any("capture provenance unavailable" in r for r in report.rules[0].reasons)
        assert None in (report.current.capture_gaps, report.baseline.capture_gaps)


def test_trees_projected_under_different_rules_are_not_compared():
    def raw():
        return RawTrace(
            trace=Trace(trace_id="t", source_kind="otlp", causal_fidelity=CausalFidelity.DECLARED_DAG,
                        links_preserved=True, capture=Capture(gaps=[])),
            steps=[Step(step_id=s, trace_id="t", seq=i, kind=TOOL, name=s) for i, s in enumerate("abc")],
            causal_edges=[Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.SPAN_LINK),
                          Edge(type=EdgeType.CAUSED_BY, src="c", dst="b", origin=EdgeOrigin.GRAPH_PARENT)],
        )
    current, legacy = normalize(raw()), normalize(raw(), origin_priority=False)
    report = _run(current, legacy, SHAPE)
    assert (report.current.projection_rule, report.baseline.projection_rule) == ("origin_priority", "temporal")
    assert _one(report) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(current, current, SHAPE)) == "PASS"


def test_empty_traces_are_insufficient():
    empty = _trace("e", [])
    assert _one(_run(empty, empty, FAILURES)) == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("code", ["langgraph.malformed_pending_writes", "langgraph.undecided_interrupt",
                                  "otlp.from_a_newer_writer"])
def test_a_gap_that_can_hide_failures_blocks_the_side_it_is_on(code):
    def lg(status, gaps=None):
        return _trace("t", [("x", TOOL, "call_tool", status)], source="langgraph", links=None, gaps=gaps)
    assert _one(_run(lg(OK, {code: 1}), lg(OK), FAILURES)) == "INSUFFICIENT_EVIDENCE"   # PASS impossible
    assert _one(_run(lg(ERR), lg(OK, {code: 1}), FAILURES)) == "INSUFFICIENT_EVIDENCE"  # FAIL impossible
    assert _one(_run(lg(ERR, {code: 1}), lg(OK), FAILURES)) == "FAIL"


def test_a_gap_without_failure_effect_does_not_block():
    gapped = _trace("t", [("x", TOOL, "call_tool", OK)], gaps={"otlp.unreadable_total_cost": 1})
    assert _one(_run(gapped, gapped, FAILURES)) == "PASS"


def test_a_langgraph_pass_states_what_still_stands_behind_it():
    def lg(status):
        return _trace("t", [("x", TOOL, "call_tool", status)], source="langgraph", links=None)
    report = _run(lg(OK), lg(OK), FAILURES)
    assert _one(report) == "PASS"
    assert report.rules[0].limitations == [check.LANGGRAPH_LIMITATION]
    assert _run(lg(ERR), lg(OK), FAILURES).rules[0].limitations == []


class _State(TypedDict):
    log: Annotated[list[str], add]


def _langgraph_run() -> NormalizedTrace:
    graph = StateGraph(_State)
    graph.add_node("a", lambda state: {"log": ["a"]})
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": []}, config)
    return normalize(LangGraphCheckpointAdapter(saver).ingest("t"))


def test_a_real_langgraph_run_is_decided_where_its_evidence_allows():
    run = _langgraph_run()
    outcomes = _outcomes(_run(run, run, FAILURES, TOPOLOGY, SHAPE))
    # Checkpoint names are inferred from routing, so labels cannot be compared; shape can.
    assert outcomes == {FAILURES: "PASS", TOPOLOGY + ":labels": "INSUFFICIENT_EVIDENCE", TOPOLOGY + ":shape": "PASS"}


def test_scheduled_steps_that_may_be_missing_block_topology():
    sched = "sha256:" + "a" * 64
    def graph_read(gaps=None):
        nt = _trace("t", [("x", TOOL, "call_tool", OK)], source="langgraph", links=None, gaps=gaps)
        nt.trace.schedule_graph = ScheduleGraph(fingerprint=sched)
        return nt
    assert _one(_run(graph_read(), graph_read(), SHAPE)) == "PASS"
    assert _one(_run(graph_read({"langgraph.unapplied_command_goto": 1}), graph_read(), SHAPE)) == "INSUFFICIENT_EVIDENCE"
    plain = _trace("t", [("x", TOOL, "call_tool", OK)], source="langgraph", links=None)
    assert _one(_run(graph_read(), plain, SHAPE)) == "INSUFFICIENT_EVIDENCE"


# --- the report contract ----------------------------------------------------------------------


def test_every_outcome_carries_the_rule_identity_and_preset_version():
    cur, base = _pair(OK)
    unset = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", UNSET)], [("t", "a")])
    for current in (cur, unset):
        [rule] = _run(current, base, pattern("tool-retry-failure")).rules
        assert (rule.rule_id, rule.rule_version, rule.params) == (
            "pattern-not-increased", 1, {"pattern_version": 2, "preset": "tool-retry-failure"})


def test_the_report_binds_both_digests_and_is_deterministic():
    cur, base = _pair(ERR)
    first = check.dumps(_run(cur, base, FAILURES, SHAPE))
    assert first == check.dumps(_run(cur, base, SHAPE, FAILURES, FAILURES))
    payload = json.loads(first)
    assert payload["current"]["artifact_digest"] == analyze(cur).artifact_digest
    assert payload["baseline"]["artifact_digest"] == analyze(base).artifact_digest
    assert check.CheckReport.model_validate(payload).model_dump(mode="json") == payload
    Draft202012Validator.check_schema(SCHEMA)


def test_redaction_is_disclosed_only_when_an_alias_survives():
    base = _trace("b", [("t", TOOL, "look up the user", OK)])
    cur = _trace("c", [("t", TOOL, "look up the user", ERR)])
    report = _run(cur, base, FAILURES)
    assert report.rules[0].evidence[0].signature.name.startswith("redacted:")
    assert check.REDACTION_WARNING in report.warnings
    assert _run(*_pair(ERR), FAILURES).warnings == []


@pytest.mark.parametrize("text", ["nope", "pattern-not-increased", "pattern-not-increased:unknown",
                                  "failures-not-increased:x", "topology-unchanged:labels"])
def test_unknown_rules_are_refused(text):
    with pytest.raises(ValueError):
        check.parse_rules([text])


# --- the command ------------------------------------------------------------------------------


@pytest.fixture
def files(tmp_path):
    cur, base = _pair(ERR)
    artifact.save_atomic(cur, tmp_path / "cur.json")
    artifact.save_atomic(base, tmp_path / "base.json")
    unset = _trace("c", [("a", AGENT, "agent", OK), ("t", TOOL, "search", UNSET)], [("t", "a")])
    artifact.save_atomic(unset, tmp_path / "unset.json")
    return tmp_path


def _cli(*args):
    return runner.invoke(app, ["check", *map(str, args)])


def test_exit_codes_are_distinct(files):
    base, cur, unset = files / "base.json", files / "cur.json", files / "unset.json"
    assert _cli(base, "--baseline", base, "--rule", FAILURES).exit_code == 0
    assert _cli(cur, "--baseline", base, "--rule", FAILURES).exit_code == 1
    assert _cli(unset, "--baseline", base, "--rule", FAILURES).exit_code == 3


def test_the_report_is_written_for_every_verdict(files):
    for current, verdict in (("base.json", "PASS"), ("cur.json", "FAIL"), ("unset.json", "INSUFFICIENT_EVIDENCE")):
        out = files / f"{verdict}.json"
        _cli(files / current, "--baseline", files / "base.json", "--rule", FAILURES, "--json-out", out)
        payload = json.loads(out.read_text())
        Draft202012Validator(SCHEMA).validate(payload)
        assert payload["verdict"] == verdict


def test_the_direct_otlp_path_checks_the_same_evidence(tmp_path):
    for name, kind in (("cur", "CHAIN"), ("base", "LLM")):
        (tmp_path / f"{name}.otlp.json").write_text(json.dumps(_retry_doc(kind)))
        result = runner.invoke(app, ["ingest-otlp", "--file", str(tmp_path / f"{name}.otlp.json"),
                                     "--out", str(tmp_path / f"{name}.json")])
        assert result.exit_code == 0, result.output
    for suffix in (".otlp.json", ".json"):
        out = tmp_path / f"report{suffix}"
        result = _cli(tmp_path / f"cur{suffix}", "--baseline", tmp_path / f"base{suffix}",
                      "--rule", pattern("tool-retry-failure"), "--json-out", out)
        assert result.exit_code == 1, result.output
    assert (tmp_path / "report.otlp.json").read_bytes() == (tmp_path / "report.json").read_bytes()


INVALID_CASES = [
    "corrupt current", "directory", "missing file", "unknown rule", "unknown preset", "no rule",
    "no baseline", "both stdin", "selector mismatch", "baseline selector mismatch",
    "report overwrites input",
]


def _invalid(files):
    base, cur = files / "base.json", files / "cur.json"
    corrupt = files / "corrupt.json"
    corrupt.write_text('{"schema_version": 7, "trace": ')
    return {
        "corrupt current": (corrupt, "--baseline", base, "--rule", FAILURES),
        "directory": (files, "--baseline", base, "--rule", FAILURES),
        "missing file": (files / "absent.json", "--baseline", base, "--rule", FAILURES),
        "unknown rule": (cur, "--baseline", base, "--rule", "nope"),
        "unknown preset": (cur, "--baseline", base, "--rule", "pattern-not-increased:nope"),
        "no rule": (cur, "--baseline", base),
        "no baseline": (cur, "--rule", FAILURES),
        "both stdin": ("-", "--baseline", "-", "--rule", FAILURES),
        "selector mismatch": (cur, "--baseline", base, "--rule", FAILURES, "--trace", "other"),
        "baseline selector mismatch": (cur, "--baseline", base, "--rule", FAILURES, "--baseline-trace", "other"),
        "report overwrites input": (cur, "--baseline", base, "--rule", FAILURES, "--json-out", base),
    }


def test_the_invalid_cases_are_all_listed(tmp_path):
    assert list(_invalid(tmp_path)) == INVALID_CASES


@pytest.mark.parametrize("case", INVALID_CASES)
def test_invalid_input_exits_2_and_leaves_an_existing_report(files, case):
    args = _invalid(files)[case]
    out = files / "existing.json"
    out.write_text("previous report\n")
    if "--json-out" not in args:
        args = (*args, "--json-out", out)
    result = _cli(*args)
    assert result.exit_code == 2, result.output
    assert out.read_text() == "previous report\n"
    assert (files / "base.json").exists()


def test_a_matching_selector_is_accepted(files):
    assert _cli(files / "cur.json", "--baseline", files / "base.json", "--rule", FAILURES,
                "--trace", "c", "--baseline-trace", "b").exit_code == 1


def test_existing_commands_keep_their_exits(files):
    base, cur = files / "base.json", files / "cur.json"
    assert runner.invoke(app, ["diff", str(base), str(cur)]).exit_code == 0
    assert runner.invoke(app, ["query", "tool-failure", str(cur)]).exit_code == 0
    assert runner.invoke(app, ["analyze", str(cur), "--baseline", str(base)]).exit_code == 0


# --- review round 1 -----------------------------------------------------------------------------


def _retry_by_name(first_name):
    def span(sid, name, kind, status, parent=None):
        out = {"traceId": "t", "spanId": sid, "status": {"code": status},
               "attributes": [{"key": "openinference.span.kind", "value": {"stringValue": kind}}]}
        if name is not None:
            out["name"] = name
        if parent:
            out["parentSpanId"] = parent
        return out
    return _otlp({"resourceSpans": [{"scopeSpans": [{"spans": [
        span("first", first_name, "TOOL", 1),
        span("retry", "retry:search", "CHAIN", 1, "first"),
        span("last", "search", "TOOL", 2, "retry"),
    ]}]}]})


@pytest.mark.parametrize("preset", ["tool-retry-failure", "tool-repeat-failure-heuristic"])
def test_a_back_reference_constrains_the_name_it_refers_to(preset):
    unnamed, named, other = _retry_by_name(None), _retry_by_name("search"), _retry_by_name("fetch")
    # The first tool's name is unknown, so whether it matches `search` is unknown too.
    assert _one(_run(unnamed, other, pattern(preset))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(named, unnamed, pattern(preset))) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(named, other, pattern(preset))) == "FAIL"


def test_an_unresolved_outcome_could_still_be_a_timeout():
    unset = _trace("t", [("t", TOOL, "search", UNSET)], source="phoenix_cli")
    assert _one(_run(unset, unset, pattern("timeout"))) == "INSUFFICIENT_EVIDENCE"
    resolved = _trace("t", [("t", TOOL, "search", OK)], source="phoenix_cli")
    assert _one(_run(resolved, resolved, pattern("timeout"))) == "PASS"


def test_a_real_interrupted_run_cannot_pass_a_timeout_check():
    from langgraph.types import interrupt

    graph = StateGraph(_State)
    graph.add_node("ask", lambda state: {"log": [interrupt("?")]})
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": []}, config)
    run = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    assert any(step.status is UNSET for step in run.steps)
    assert set(_outcomes(_run(run, run, FAILURES, pattern("timeout"))).values()) == {"INSUFFICIENT_EVIDENCE"}


def test_nonstandard_json_constants_are_invalid_input(files):
    payload = json.loads((files / "base.json").read_text())
    payload["unused"] = float("nan")
    nan = files / "nan.json"
    nan.write_text(json.dumps(payload))
    assert "NaN" in nan.read_text()
    result = _cli(nan, "--baseline", files / "base.json", "--rule", FAILURES)
    assert result.exit_code == 2, result.output
    # Other commands keep reading what they read before.
    assert runner.invoke(app, ["analyze", str(nan)]).exit_code == 0


def test_paths_that_cannot_be_resolved_are_invalid_input(files):
    loop = files / "loop.json"
    loop.symlink_to(loop)
    for args in ((loop, "--baseline", files / "base.json"),
                 (files / "cur.json", "--baseline", files / "base.json", "--json-out", files / ("x" * 300 + ".json"))):
        result = _cli(*args, "--rule", FAILURES)
        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output


def test_evidence_is_printed_literally(files):
    base = _trace("b", [("t", TOOL, ":ok:", OK)])
    cur = _trace("c", [("t", TOOL, ":ok:", ERR)])
    artifact.save_atomic(base, files / "emoji-base.json")
    artifact.save_atomic(cur, files / "emoji-cur.json")
    result = _cli(files / "emoji-cur.json", "--baseline", files / "emoji-base.json", "--rule", FAILURES)
    assert result.exit_code == 1
    assert '":ok:"' in result.output and "🆗" not in result.output


def _report_payload():
    cur, base = _pair(ERR)
    return json.loads(check.dumps(_run(cur, base, FAILURES, pattern("tool-failure"), SHAPE)))


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["rules"][0].update(params={"preset": "tool-failure", "pattern_version": 1}),
        lambda p: p["rules"][0].update(evidence=[{"change": "x"}]),
        lambda p: p["rules"][0].update(rule_version=2),
        lambda p: p["rules"][1].update(params={"preset": "tool-failure"}),
        lambda p: p["rules"][1].update(params={"preset": "tool-failure", "pattern_version": 0}),
        lambda p: p["rules"][2].update(params={"compare": "both"}),
        lambda p: p["rules"][2].update(evidence=[p["rules"][0]["evidence"][0]]),
        lambda p: p["rules"][0].update(rule_id="something-else"),
    ],
)
def test_the_schema_and_the_model_refuse_the_same_reports(change):
    payload = _report_payload()
    change(payload)
    with pytest.raises(ValidationError):
        Draft202012Validator(SCHEMA).validate(payload)
    with pytest.raises(ValueError):
        check.CheckReport.model_validate(payload)


def test_the_schema_and_the_model_accept_the_emitted_report():
    payload = _report_payload()
    Draft202012Validator(SCHEMA).validate(payload)
    assert check.CheckReport.model_validate(payload).model_dump(mode="json") == payload


# --- review round 2 -----------------------------------------------------------------------------


def _chain(first_name):
    return _trace("t", [("a", TOOL, first_name, OK), ("r", CHAIN, "retry:search", OK), ("b", TOOL, "search", ERR)],
                  [("r", "a"), ("b", "r")])


def test_a_missing_name_is_unknown_even_without_a_flag():
    # Hand-built or otherwise unflagged: a null name still cannot be compared.
    assert _one(_run(_chain(None), _chain("fetch"), pattern("tool-retry-failure"))) == "INSUFFICIENT_EVIDENCE"
    unnamed = _trace("b", [("t", TOOL, None, ERR)])
    named = _trace("c", [("t", TOOL, "search", ERR)])
    assert _one(_run(named, unnamed, FAILURES)) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(unnamed, unnamed, FAILURES)) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(_trace("x", [("t", TOOL, None, OK)]), _trace("x", [("t", TOOL, None, OK)]), TOPOLOGY)) \
        == "INSUFFICIENT_EVIDENCE"
    # Healthy unnamed steps cannot hide a failure.
    healthy = _trace("h", [("i", CHAIN, None, OK), ("t", TOOL, "search", OK)], [("t", "i")])
    assert _one(_run(healthy, healthy, FAILURES)) == "PASS"


def test_a_langgraph_run_without_nodes_still_cannot_compare_labels():
    graph = StateGraph(_State)
    graph.add_edge(START, END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": []}, config)
    run = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    assert _outcomes(_run(run, run, TOPOLOGY, SHAPE)) == {
        TOPOLOGY + ":labels": "INSUFFICIENT_EVIDENCE", TOPOLOGY + ":shape": "PASS"}


def test_the_model_holds_the_schema_constraints():
    cur, base = _pair(OK)
    with pytest.raises(ValueError):
        check.evaluate(cur, base, [])
    for change in (
        lambda p: p["current"].update(artifact_digest="sha256:short"),
        lambda p: p["current"].update(step_count=-1),
        lambda p: p["current"].update(schedule_graph_fingerprint="nope"),
        lambda p: p["current"].update(causal_fidelity="made_up"),
        lambda p: p["rules"][0]["evidence"][0].update(current_errors=-1),
        lambda p: p.update(rules=[]),
    ):
        payload = _report_payload()
        change(payload)
        with pytest.raises(ValidationError):
            Draft202012Validator(SCHEMA).validate(payload)
        with pytest.raises(ValueError):
            check.CheckReport.model_validate(payload)


# --- review of the merged range ---------------------------------------------------------------


@pytest.mark.parametrize("code", ["langgraph.malformed_pending_writes", "langgraph.undecided_interrupt",
                                  "otlp.from_a_newer_writer"])
def test_a_gap_that_can_hide_task_steps_blocks_topology(code):
    def lg(gaps=None):
        return _trace("t", [("x", CHAIN, None, OK)], source="langgraph", links=None, gaps=gaps)
    for pair in ((lg({code: 1}), lg()), (lg(), lg({code: 1}))):
        assert _one(_run(*pair, SHAPE)) == "INSUFFICIENT_EVIDENCE"


def test_a_task_failure_lost_to_a_malformed_write_cannot_decide_topology():
    from test_langgraph_adapter import _StubSaver, _ck

    def ingest(writes):
        return normalize(LangGraphCheckpointAdapter(_StubSaver([_ck("c1", "", 1, None, pending_writes=writes)])).ingest("A"))

    malformed = ingest([("task-x", "__error__")])            # not a (task, channel, value) triple
    recorded = ingest([("task-x", "__error__", "RuntimeError('boom')")])
    clean = ingest([])
    assert len(recorded.steps) == len(malformed.steps) + 1  # the recorded write adds a task step
    assert _one(_run(malformed, clean, SHAPE)) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(recorded, malformed, SHAPE)) == "INSUFFICIENT_EVIDENCE"
    assert _one(_run(recorded, clean, SHAPE)) == "FAIL"
