"""``--redact-errors``: error text stays out of OTLP and LangGraph artifacts, nothing else moves.

Each ingest runs twice, plain and redacted. The redacted artifact must be the plain one with
every ERROR step's message dropped and ``error_msg`` listed as unknown: status, names, edges,
capture gaps and warnings all unchanged, so redaction can never pass for a different run.
"""

import json
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from typer.testing import CliRunner
from tiny_agent import run

from test_langgraph_adapter import _StubSaver, _ck
from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.cli import app
from tracegraph.model import NormalizedTrace, RawTrace, StepStatus

runner = CliRunner()
SENTINEL = "REDACT_SENTINEL_sk-live-123"


def _redacted_view(trace):
    """What redacting ``trace`` must produce: its ERROR steps without their messages."""
    steps = [
        s.model_copy(update={"error_msg": None,
                             "unknown_fields": sorted({*s.unknown_fields, "error_msg"})})
        if s.status is StepStatus.ERROR else s
        for s in trace.steps
    ]
    return trace.model_copy(update={"steps": steps})


def _assert_only_messages_differ(plain: NormalizedTrace, redacted: NormalizedTrace):
    assert any(s.status is StepStatus.ERROR for s in plain.steps), "nothing failed: vacuous"
    assert artifact.dumps(_redacted_view(plain)) == artifact.dumps(redacted)


def _ingest_pair(args, tmp_path):
    plain, redacted = tmp_path / "plain.json", tmp_path / "redacted.json"
    for out, extra in ((plain, []), (redacted, ["--redact-errors"])):
        res = runner.invoke(app, [*args, "--out", str(out), *extra])
        assert res.exit_code == 0, res.output
    return plain, redacted


# --- OTLP ---


def _span(span_id, name, kind, status, *, offset, parent="root", events=None):
    span = {"traceId": "t", "spanId": span_id, "name": name, "status": status,
            "startTimeUnixNano": str(1700000000000000000 + offset),
            "attributes": [{"key": "openinference.span.kind", "value": {"stringValue": kind}}]}
    if span_id != parent:
        span["parentSpanId"] = parent
    if events:
        span["events"] = events
    return span


def _otlp_file(tmp_path, message="connection timeout " + SENTINEL, *, bare=True):
    exception = {"name": "exception", "attributes": [
        {"key": "exception.message", "value": {"stringValue": SENTINEL + " from an event"}}]}
    spans = [
        _span("root", "agent", "AGENT", {"code": 2, "message": message}, offset=0, parent="root"),
        _span("tool", "search", "TOOL", {"code": 0}, offset=100, events=[exception]),
        # ERROR with no message: the adapter already substitutes "error" and marks it unknown.
        _span("bare", "fetch", "TOOL", {"code": 2}, offset=200),
        _span("fine", "plan", "CHAIN", {"code": 1}, offset=300),
    ]
    if not bare:
        spans = [s for s in spans if s["spanId"] != "bare"]
    path = tmp_path / "spans.json"
    path.write_text(json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}))
    return path


def test_ingest_otlp_redact_errors_drops_only_the_messages(tmp_path):
    plain, redacted = _ingest_pair(["ingest-otlp", "--file", str(_otlp_file(tmp_path))], tmp_path)
    assert plain.read_text().count(SENTINEL) == 2  # the control: the text was there to drop
    assert SENTINEL not in redacted.read_text()
    _assert_only_messages_differ(artifact.load(plain), artifact.load(redacted))
    steps = {s.step_id: s for s in artifact.load(redacted).steps}
    assert {sid: (s.status.value, s.error_msg, s.unknown_fields) for sid, s in steps.items()} == {
        "root": ("error", None, ["error_msg"]),
        "tool": ("error", None, ["error_msg"]),
        "bare": ("error", None, ["error_msg"]),
        "fine": ("ok", None, []),
    }


def test_redacted_otlp_artifact_prints_no_error_text(tmp_path):
    _, redacted = _ingest_pair(["ingest-otlp", "--file", str(_otlp_file(tmp_path))], tmp_path)
    for args in (["inspect", str(redacted)], ["explain", str(redacted), "tool"]):
        res = runner.invoke(app, args)
        assert res.exit_code == 0, res.output
        assert SENTINEL not in res.output and "search" in res.output


def test_a_redacted_run_cannot_rule_out_a_timeout(tmp_path):
    # The message is what the timeout preset reads, so withholding it leaves the rule
    # undecided rather than passing; a preset that reads no message still decides. No bare
    # span here: its substituted message would leave the plain run undecided too.
    source = _otlp_file(tmp_path, message="connection reset", bare=False)
    plain, redacted = _ingest_pair(["ingest-otlp", "--file", str(source)], tmp_path)

    def check(current, rule):
        return runner.invoke(app, ["check", str(current), "--baseline", str(plain), "--rule", rule])

    assert check(plain, "pattern-not-increased:timeout").exit_code == 0
    assert check(redacted, "pattern-not-increased:timeout").exit_code == 3
    assert check(redacted, "pattern-not-increased:tool-failure").exit_code == 0


# --- LangGraph ---


def test_ingest_redact_errors_drops_the_error_channel_value(tmp_path):
    db = tmp_path / "trace.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        run(saver, "A", "boom-please")
    plain, redacted = _ingest_pair(["ingest", "--sqlite", str(db), "--thread", "A"], tmp_path)
    assert "tool failed" in plain.read_text()
    assert "tool failed" not in redacted.read_text()
    _assert_only_messages_differ(artifact.load(plain), artifact.load(redacted))


class _State(TypedDict):
    x: int


def test_ingest_redact_errors_drops_a_node_exception(tmp_path):
    def call_tool(state):
        raise RuntimeError(SENTINEL)

    graph = StateGraph(_State)
    graph.add_node("plan", lambda state: {"x": 1})
    graph.add_node("call_tool", call_tool)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "call_tool")
    graph.add_edge("call_tool", END)
    db = tmp_path / "trace.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        try:
            graph.compile(checkpointer=saver).invoke({"x": 0}, {"configurable": {"thread_id": "t"}})
        except RuntimeError:
            pass
    plain, redacted = _ingest_pair(["ingest", "--sqlite", str(db), "--thread", "t"], tmp_path)
    assert SENTINEL in plain.read_text()
    assert SENTINEL not in redacted.read_text()
    _assert_only_messages_differ(artifact.load(plain), artifact.load(redacted))


def _raw_pair(tuples) -> tuple[RawTrace, RawTrace]:
    return (LangGraphCheckpointAdapter(_StubSaver(tuples)).ingest("A"),
            LangGraphCheckpointAdapter(_StubSaver(tuples), include_error_messages=False).ingest("A"))


def _assert_raw_only_messages_differ(plain: RawTrace, redacted: RawTrace):
    assert any(s.status is StepStatus.ERROR for s in plain.steps), "nothing failed: vacuous"
    assert _redacted_view(plain).model_dump() == redacted.model_dump()
    assert plain.ingest_warnings == redacted.ingest_warnings


def test_redaction_keeps_cancellation_and_disclosures():
    # One task raised and its sibling was cancelled through the same channel; neither task
    # id resolves to a node, so both are unattributed and the read discloses the failure.
    cp = _ck("1f1ae6ea-e7d8-6c28-8001-03afbf35e17c", "", 1, None, pending_writes=[
        ("task-a", "__error__", f"RuntimeError('{SENTINEL}')"),
        ("task-b", "__error__", "CancelledError()"),
    ])
    plain, redacted = _raw_pair([cp])
    _assert_raw_only_messages_differ(plain, redacted)
    by_status = {s.status: s for s in redacted.steps if "#task:" in s.step_id}
    assert by_status[StepStatus.ERROR].unknown_fields == ["error_msg", "kind", "name"]
    # A cancelled task has no message to withhold, so redaction adds nothing to it.
    assert by_status[StepStatus.UNSET].unknown_fields == ["kind", "name"]
    assert by_status[StepStatus.UNSET].error_msg is None
    assert any("could not be attributed" in w for w in redacted.ingest_warnings)
    assert SENTINEL not in redacted.model_dump_json()


def test_redaction_lists_an_already_unknown_message_once():
    # The checkpointer kept no exception text; the placeholder is already marked unknown.
    tuples = [_ck("1f1ae6ea-e7d8-6c28-8001-03afbf35e17c", "", 1, None,
                  pending_writes=[("task-x", "__error__", None)])]
    plain, redacted = _raw_pair(tuples)
    _assert_raw_only_messages_differ(plain, redacted)
    [failed] = [s for s in redacted.steps if s.status is StepStatus.ERROR]
    assert failed.error_msg is None
    assert failed.unknown_fields == ["error_msg", "kind", "name"]


class _RetryState(TypedDict, total=False):
    error: str
    log: list


def test_redaction_keeps_the_rewritten_error_disclosure():
    # The handler rewrites the error while it is still set: disclosed as a gap, not a failure.
    graph = StateGraph(_RetryState)
    graph.add_node("call_tool", lambda s: {"error": SENTINEL})
    graph.add_node("handler", lambda s: {"error": "handled: " + s["error"]})
    graph.add_edge(START, "call_tool")
    graph.add_edge("call_tool", "handler")
    graph.add_edge("handler", END)
    with SqliteSaver.from_conn_string(":memory:") as saver:
        graph.compile(checkpointer=saver).invoke({}, {"configurable": {"thread_id": "t"}})
        plain = LangGraphCheckpointAdapter(saver).ingest("t")
        redacted = LangGraphCheckpointAdapter(saver, include_error_messages=False).ingest("t")
    _assert_raw_only_messages_differ(plain, redacted)
    assert redacted.trace.capture is not None
    assert {g.code: g.count for g in redacted.trace.capture.gaps} == {
        "langgraph.rewritten_error_channel": 1
    }
    assert SENTINEL not in redacted.model_dump_json()
