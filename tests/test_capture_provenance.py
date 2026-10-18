"""Capture provenance (artifact v7): what an adapter could not observe is kept in the artifact.

``Trace.capture`` records every ingestion disclosure as a fixed code and count, and
``Step.unknown_fields`` marks values an adapter defaulted or substituted. Both are written only
by adapters, so an artifact from an older build loads, re-serializes and digests as before.
"""

from __future__ import annotations

import ast
import json
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest
from test_langgraph_adapter import _StubSaver, _ck
from test_otlp_adapter import _loop_doc

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter, OTLPSpanAdapter, PhoenixExportAdapter
from tracegraph.model import (
    CAPTURE_GAP_CODES,
    Capture,
    NormalizedTrace,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)
from tracegraph.normalize import normalize

ADAPTERS = Path(__file__).parents[1] / "src" / "tracegraph" / "adapters"
SYNCMILL = Path(__file__).parent / "fixtures" / "syncmill"


def without_provenance(nt: NormalizedTrace) -> NormalizedTrace:
    """``nt`` as a build without capture provenance would have produced it."""
    return nt.model_copy(
        update={
            "trace": nt.trace.model_copy(update={"capture": None}),
            "steps": [step.model_copy(update={"unknown_fields": []}) for step in nt.steps],
        }
    )


def gap_counts(trace: RawTrace | NormalizedTrace) -> dict[str, int]:
    assert trace.trace.capture is not None
    return {gap.code: gap.count for gap in trace.trace.capture.gaps}


def _unknown(raw: RawTrace) -> dict[str, list[str]]:
    return {step.step_id: step.unknown_fields for step in raw.steps if step.unknown_fields}


def _span(sid, *, name="s", kind="TOOL", status=None, events=None, parent=None):
    span = {"traceId": "t", "spanId": sid, "name": name, "startTimeUnixNano": "1",
            "attributes": [] if kind is None else [
                {"key": "openinference.span.kind", "value": {"stringValue": kind}}
            ]}
    if status is not None:
        span["status"] = status
    if events is not None:
        span["events"] = events
    if parent is not None:
        span["parentSpanId"] = parent
    return span


def _doc(*spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


# --- the audit: no disclosure without a code ------------------------------------------------


def _warning_writes(tree: ast.AST) -> list[int]:
    """Lines that add to a warnings list directly instead of through ``Disclosures.add``."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if node.func.attr in {"append", "extend", "insert"} and "warning" in name:
                lines.append(node.lineno)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if "warning" in name:
                    lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize("module", sorted(p.name for p in ADAPTERS.glob("*.py") if p.name != "base.py"))
def test_every_adapter_disclosure_goes_through_a_code(module):
    assert _warning_writes(ast.parse((ADAPTERS / module).read_text())) == []


def test_every_gap_code_is_emitted_somewhere_and_nothing_else_is():
    emitted = set()
    for path in ADAPTERS.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add" and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                emitted.add(node.args[0].value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("otlp.unreadable_"):
                emitted.add(node.value)
    assert emitted == CAPTURE_GAP_CODES


def test_the_audit_catches_a_bypass():
    tree = ast.parse(
        "self.ingest_warnings.append('x')\nraw.ingest_warnings.extend(y)\nwarnings = list()\n"
        "warnings.append('uncoded')\nraw.ingest_warnings = texts\n"
    )
    assert _warning_writes(tree) == [1, 2, 3, 4, 5]


def test_disclosure_text_cannot_be_added_without_a_code():
    from tracegraph.adapters.base import Disclosures

    disclosures = Disclosures()
    with pytest.raises(AttributeError):
        disclosures.texts.append("uncoded")  # type: ignore[attr-defined]


# --- clean ingests record an empty capture --------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in SYNCMILL.glob("*.otlp.json")))
def test_clean_otlp_fixtures_record_a_complete_capture(name):
    adapter = OTLPSpanAdapter.from_file(SYNCMILL / name)
    nt = normalize(adapter.ingest(adapter.discover()[0]))
    assert nt.trace.capture == Capture(gaps=[])
    assert not any(step.unknown_fields for step in nt.steps)
    payload = json.loads(artifact.dumps(nt))
    assert payload["schema_version"] == 7
    assert payload["trace"]["trace"]["capture"] == {"gaps": []}
    assert all("unknown_fields" not in step for step in payload["trace"]["steps"])


# --- OTLP substitutions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, flagged",
    [("TOOL", False), ("tool", False), ("UNKNOWN", False), (None, True), ("WEIRD", True)],
)
def test_otlp_kind_is_unknown_only_when_defaulted(kind, flagged):
    raw = OTLPSpanAdapter(_doc(_span("a", kind=kind))).ingest("t")
    assert ("kind" in raw.steps[0].unknown_fields) is flagged
    if flagged:
        assert raw.steps[0].kind is StepKind.CHAIN


@pytest.mark.parametrize(
    "status, events, flagged, message",
    [
        ({"code": 2}, None, True, "error"),
        ({"code": 2, "message": "boom"}, None, False, "boom"),
        ({"code": 2}, [{"name": "exception", "attributes": [
            {"key": "exception.type", "value": {"stringValue": "TimeoutError"}}]}], False, "TimeoutError"),
        ({"code": 0}, [{"name": "exception", "attributes": [
            {"key": "exception.message", "value": {"stringValue": "late"}}]}], False, "late"),
    ],
    ids=["fallback", "message", "exception-on-error", "exception-on-unset"],
)
def test_otlp_error_message_is_unknown_only_when_substituted(status, events, flagged, message):
    raw = OTLPSpanAdapter(_doc(_span("a", status=status, events=events))).ingest("t")
    step = raw.steps[0]
    assert step.status is StepStatus.ERROR
    assert step.error_msg == message
    assert ("error_msg" in step.unknown_fields) is flagged


def test_withheld_messages_are_unknown_on_error_steps_only():
    doc = _doc(_span("a", status={"code": 2, "message": "boom"}),
               _span("b", status={"code": 1}, parent="a"))
    raw = OTLPSpanAdapter(doc, include_error_messages=False).ingest("t")
    assert _unknown(raw) == {"a": ["error_msg"]}


@pytest.mark.parametrize("name", [None, ""])
def test_otlp_span_without_a_name_has_an_unknown_name(name):
    span = _span("a")
    span["name"] = name
    raw = OTLPSpanAdapter(_doc(span)).ingest("t")
    assert raw.steps[0].unknown_fields == ["name"]


def test_otlp_unknown_fields_are_sorted_and_combined():
    span = _span("a", kind=None, status={"code": 2})
    del span["name"]
    raw = OTLPSpanAdapter(_doc(span)).ingest("t")
    assert raw.steps[0].unknown_fields == ["error_msg", "kind", "name"]


def test_logical_parent_chosen_by_timing_is_a_gap():
    raw = OTLPSpanAdapter(_loop_doc(1_000_000_000)).ingest("t")
    assert gap_counts(raw) == {"otlp.graph_parent_by_timing": 1}


@pytest.mark.parametrize("p1_start", ["yesterday", None])
def test_logical_parent_not_honored_is_a_gap(p1_start):
    raw = OTLPSpanAdapter(_loop_doc(p1_start)).ingest("t")
    counts = gap_counts(raw)
    assert counts["otlp.graph_parent_fallback"] == 1
    assert "otlp.graph_parent_by_timing" not in counts


def test_unique_logical_parent_is_not_a_gap():
    attr = lambda k, v: {"key": k, "value": {"stringValue": v}}  # noqa: E731
    doc = _doc(
        {"traceId": "t", "spanId": "p", "name": "p", "attributes": [attr("graph.node.id", "P")]},
        {"traceId": "t", "spanId": "c", "name": "c", "parentSpanId": "p",
         "attributes": [attr("graph.node.parent_id", "P")]},
    )
    assert gap_counts(OTLPSpanAdapter(doc).ingest("t")) == {}


def test_unreadable_timestamps_are_counted():
    a, b = _span("a"), _span("b", parent="a")
    a["startTimeUnixNano"] = "soon"
    b["endTimeUnixNano"] = "later"
    raw = OTLPSpanAdapter(_doc(a, b)).ingest("t")
    assert gap_counts(raw) == {"otlp.unreadable_timestamp": 2}
    assert len(raw.ingest_warnings) == 1


# --- Phoenix ----------------------------------------------------------------------------------


def _phoenix(*spans):
    return {"traceId": "t", "spans": list(spans)}


def _phoenix_span(sid, *, status="OK", kind="TOOL", name="call", score=None):
    span = {"context": {"trace_id": "t", "span_id": sid}, "name": name, "span_kind": kind,
            "status_code": status, "start_time": "2026-01-01T00:00:00Z", "attributes": {}}
    if score is not None:
        span["annotations"] = [{"name": "q", "result": {"score": score}}]
    return span


def test_phoenix_error_messages_are_always_unknown():
    raw = PhoenixExportAdapter(_phoenix(_phoenix_span("a", status="ERROR"))).ingest("t")
    assert raw.steps[0].unknown_fields == ["error_msg"]
    assert raw.steps[0].error_msg is None


def test_phoenix_drops_non_identifier_names_as_unknown():
    raw = PhoenixExportAdapter(_phoenix(_phoenix_span("a", name="look up the user"))).ingest("t")
    assert raw.steps[0].name is None
    assert raw.steps[0].unknown_fields == ["name"]


# --- the contract -----------------------------------------------------------------------------


def _nt(**step_fields) -> NormalizedTrace:
    return normalize(RawTrace(
        trace=Trace(trace_id="t", source_kind="x", capture=Capture(gaps=[])),
        steps=[Step(step_id="a", trace_id="t", seq=0, **step_fields)],
    ))


def test_older_artifacts_keep_their_bytes():
    old = without_provenance(_nt())
    text = artifact.dumps(old)
    assert json.loads(text)["schema_version"] == 2
    assert "capture" not in text and "unknown_fields" not in text
    assert artifact.dumps(artifact.loads(text)) == text


def test_v7_round_trips_byte_for_byte():
    nt = _nt(status=StepStatus.ERROR, error_msg="error", unknown_fields=["error_msg", "kind"])
    nt.trace.capture = Capture.from_counts({"otlp.unreadable_timestamp": 3})
    text = artifact.dumps(nt)
    assert json.loads(text)["schema_version"] == 7
    assert artifact.dumps(artifact.loads(text)) == text


def test_capture_requires_version_7():
    payload = json.loads(artifact.dumps(_nt(unknown_fields=["kind"])))
    payload["schema_version"] = 6
    with pytest.raises(ValueError, match="declares schema_version 6 but its content requires 7"):
        artifact.from_obj(payload)


def test_unknown_fields_alone_still_require_version_7():
    # Validation refuses unknown_fields without a capture record, but a model changed after
    # validation can still hold them; the version rule must not let an older reader drop them.
    nt = _nt(unknown_fields=["kind"])
    nt.trace.capture = None
    assert artifact._required_version(nt) == 7


def _reordered_flags(nt):
    nt.steps[0].unknown_fields.extend(["name", "kind"])


def _duplicated_gap(nt):
    nt.trace.capture.gaps.append(nt.trace.capture.gaps[0])


def _bad_code(nt):
    nt.trace.capture.gaps[0].code = "Not A Code"


def _set_flags(value):
    def mutate(nt):
        nt.steps[0].unknown_fields = value
    mutate.__name__ = f"flags={value!r}"
    return mutate


def _set_capture(nt):
    nt.trace.capture = {}


@pytest.mark.parametrize(
    "mutate",
    [_reordered_flags, _duplicated_gap, _bad_code, _set_capture,
     *(_set_flags(v) for v in (None, {}, "", False, "kind", ["status"]))],
    ids=lambda f: f.__name__,
)
@pytest.mark.parametrize(
    "write",
    [artifact.dumps,
     lambda nt: artifact.save(nt, Path("unused")),
     lambda nt: artifact.save_atomic(nt, Path("unused")),
     lambda nt: __import__("tracegraph.normalize").normalize.validate_normalized(nt)],
    ids=["dumps", "save", "save_atomic", "validate_normalized"],
)
# The writers validate the model first, and pydantic's serializer warns on the bad value.
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_provenance_changed_after_validation_is_refused_before_writing(mutate, write, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    nt = _nt(unknown_fields=[])
    nt.trace.capture = Capture.from_counts({"otlp.unreadable_timestamp": 1})
    mutate(nt)
    with pytest.raises(ValueError):
        write(nt)
    assert list(tmp_path.iterdir()) == []


def test_the_writer_refuses_flags_its_reader_would_reject(tmp_path):
    nt = _nt(unknown_fields=["kind"])
    nt.trace.capture = None
    with pytest.raises(ValueError, match="no capture record"):
        artifact.dumps(nt)
    with pytest.raises(ValueError, match="no capture record"):
        artifact.save(nt, tmp_path / "a.json")
    assert not (tmp_path / "a.json").exists()


def test_older_reader_refuses_version_7_by_name(monkeypatch):
    text = artifact.dumps(_nt())
    monkeypatch.setattr(artifact, "_READABLE_VERSIONS", (1, 2, 3, 4, 5, 6))
    with pytest.raises(ValueError, match="unsupported artifact schema_version 7"):
        artifact.loads(text)


def _with(payload, path, value):
    target = payload["trace"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return payload


@pytest.mark.parametrize(
    "path, value, match",
    [
        (("trace", "capture"), None, "no capture record"),
        (("trace", "capture"), [], "valid dictionary"),
        (("trace", "capture"), {}, "gaps"),
        (("trace", "capture", "gaps"), [{"code": "Not A Code", "count": 1}], "pattern"),
        (("trace", "capture", "gaps"), [{"code": "otlp.unreadable_timestamp", "count": 0}], "greater than or equal to 1"),
        (("trace", "capture", "gaps"), [{"code": "otlp.unreadable_total_cost", "count": 1},
                                        {"code": "otlp.unreadable_timestamp", "count": 1}], "sorted by code"),
        (("trace", "capture", "gaps"), [{"code": "otlp.unreadable_timestamp", "count": 1},
                                        {"code": "otlp.unreadable_timestamp", "count": 1}], "one entry per code"),
        (("steps", 0, "unknown_fields"), ["name", "kind"], "sorted"),
        (("steps", 0, "unknown_fields"), ["kind", "kind"], "duplicates"),
        (("steps", 0, "unknown_fields"), ["status"], "error_msg"),
        (("steps", 0, "unknown_fields"), "kind", "valid list"),
    ],
)
def test_malformed_provenance_is_a_value_error(path, value, match):
    payload = json.loads(artifact.dumps(_nt(unknown_fields=["kind"])))
    with pytest.raises(ValueError, match=match):
        artifact.from_obj(_with(payload, path, value))


def test_a_gap_code_from_a_newer_writer_is_kept():
    payload = json.loads(artifact.dumps(_nt()))
    payload["trace"]["trace"]["capture"]["gaps"] = [{"code": "otlp.new_disclosure", "count": 2}]
    nt = artifact.from_obj(payload)
    assert gap_counts(nt) == {"otlp.new_disclosure": 2}
    assert json.loads(artifact.dumps(nt)) == payload


def test_an_adapter_cannot_emit_an_undefined_code():
    with pytest.raises(ValueError, match="unknown capture gap code"):
        Capture.from_counts({"otlp.new_disclosure": 1})


def test_unknown_fields_without_capture_cannot_be_built():
    with pytest.raises(ValueError, match="no capture record"):
        RawTrace(trace=Trace(trace_id="t", source_kind="x"),
                 steps=[Step(step_id="a", trace_id="t", seq=0, unknown_fields=["kind"])])


def test_ladybug_round_trips_provenance_byte_for_byte(tmp_path):
    pytest.importorskip("ladybug")
    from tracegraph.store.ladybug import LadybugStore

    nt = _nt(status=StepStatus.ERROR, error_msg="error", unknown_fields=["error_msg", "kind", "name"])
    nt.trace.capture = Capture.from_counts({"langgraph.undecided_interrupt": 2})
    with LadybugStore.from_trace(nt) as store:
        store.export_artifact(tmp_path / "out.json")
    assert (tmp_path / "out.json").read_text() == artifact.dumps(nt)


# --- LangGraph ----------------------------------------------------------------------------------


def _input(cid):
    t = _ck(cid, "", -1, None)
    return t._replace(metadata={**t.metadata, "source": "input"})


@pytest.mark.parametrize("values", [{"error": "boom"}, {}], ids=["failed", "healthy"])
def test_unattributed_checkpoint_has_unknown_kind_and_name_whatever_its_status(values):
    first = _ck("c1", "", 0, "c0")
    unnamed = _ck("c2", "", 1, "c1", channel_values=values)
    raw = LangGraphCheckpointAdapter(_StubSaver([unnamed, first, _input("c0")])).ingest("A")
    # The input checkpoint and the first loop after it have no producing node at all.
    assert _unknown(raw) == {"c2": ["kind", "name"]}


def test_history_without_its_input_checkpoint_is_unattributed():
    before = _ck("c0", "", 0, None)
    failed = _ck("c1", "", 1, "c0", channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(_StubSaver([failed, before])).ingest("A")
    assert _unknown(raw) == {"c0": ["kind", "name"], "c1": ["kind", "name"]}


def test_a_named_checkpoint_keeps_its_label_but_the_name_is_inferred():
    first = _ck("c1", "", 0, "c0", channel_values={"branch:to:call_tool": True})
    failed = _ck("c2", "", 1, "c1", channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(_StubSaver([failed, first, _input("c0")])).ingest("A")
    [step] = [s for s in raw.steps if s.status is StepStatus.ERROR]
    # The routing channel names the node; the checkpoint itself does not record it.
    assert (step.name, step.kind, step.unknown_fields) == ("call_tool", StepKind.TOOL, ["kind", "name"])
    assert _unknown(raw) == {"c2": ["kind", "name"]}


class _State(TypedDict):
    log: Annotated[list[str], add]


def test_only_the_input_checkpoints_are_exempt_in_a_clean_run():
    graph = StateGraph(_State)
    graph.add_node("a", lambda state: {"log": ["a"]})
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": []}, config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    # Input and first-loop checkpoints have no producing node: a known absence, not a gap. The
    # checkpoint `a` wrote is named from routing, which is an inference.
    assert [(s.source, s.name, s.unknown_fields) for s in raw.steps] == [
        (StepSource.INPUT, None, []),
        (StepSource.LOOP, None, []),
        (StepSource.LOOP, "a", ["kind", "name"]),
    ]
    assert gap_counts(raw) == {}


class _ErrorState(TypedDict):
    log: Annotated[list[str], add]
    error: str | None


def test_an_error_supplied_as_input_is_not_an_attribution_gap():
    graph = StateGraph(_ErrorState)
    graph.add_node("a", lambda state: {"log": ["a"]})
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": [], "error": "from the caller"}, config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    [failed] = [s for s in raw.steps if s.status is StepStatus.ERROR]
    assert (failed.source, failed.name, failed.unknown_fields) == (StepSource.LOOP, None, [])


def test_a_node_launched_by_send_is_unattributed():
    from langgraph.types import Send

    graph = StateGraph(_State)
    graph.add_node("call_tool", lambda state: {"log": ["t"]})
    graph.add_conditional_edges(START, lambda state: [Send("call_tool", state)])
    graph.add_edge("call_tool", END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": []}, config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    assert [(s.seq, s.name, s.unknown_fields) for s in raw.steps] == [
        (-1, None, []), (0, None, []), (1, None, ["kind", "name"]),
    ]


def test_parallel_nodes_do_not_lend_their_name_to_a_failure():
    graph = StateGraph(_ErrorState)
    graph.add_node("a", lambda state: {"log": ["a"]})
    graph.add_node("call_tool", lambda state: {"log": ["t"], "error": "boom"})
    graph.add_edge(START, "a")
    graph.add_edge(START, "call_tool")
    graph.add_edge(["a", "call_tool"], END)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t"}}
    graph.compile(checkpointer=saver).invoke({"log": [], "error": None}, config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    [failed] = [s for s in raw.steps if s.status is StepStatus.ERROR]
    # Routing names one of the two parallel nodes; which one failed is not recorded.
    assert failed.unknown_fields == ["kind", "name"]


def test_an_update_from_the_input_checkpoint_is_not_exempt():
    graph = StateGraph(_ErrorState)
    graph.add_node("call_tool", lambda state: {"log": ["t"]})
    graph.add_edge(START, "call_tool")
    graph.add_edge("call_tool", END)
    saver = InMemorySaver()
    app = graph.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "t"}}
    app.invoke({"log": [], "error": None}, config)
    first = next(t for t in saver.list(config) if t.metadata.get("source") == "input")
    app.update_state(first.config, {"log": ["manual"], "error": "boom"}, as_node="call_tool")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    [update] = [s for s in raw.steps if s.source is StepSource.UPDATE]
    assert update.unknown_fields == ["kind", "name"]
