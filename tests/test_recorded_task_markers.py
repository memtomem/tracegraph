"""Interrupted, scheduled and undecided-interrupt tasks are shown by ``analyze`` and the memo.

The trace status means "a recorded step failed", so a paused or stopped run reads ``ok`` unless
a step failed. The markers say what the checkpoints record and nothing more: a revised graph can
finish past an interrupt record, so the wording never claims the run did or did not complete.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
import pytest

from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis.diagnose import TRACE_STATUS_SCOPE, analyze, dumps
from tracegraph.analysis.memo import build_incident_memo
from tracegraph.model import StepStatus
from tracegraph.normalize import normalize

from test_langgraph_interrupts import CONFIG, _append, _asks, _fan_out, _graph
from test_langgraph_scheduled import _looping

SCHEMA = Path(__file__).parents[1] / "contracts" / "analysis-report.schema.json"
DIGEST = "sha256:" + "c" * 64


def _ingest(saver, graph=None):
    return normalize(LangGraphCheckpointAdapter(saver, schedule_graph=graph).ingest("t"))


def _warning(label, markers):
    return f"{label}: recorded task markers: {markers}; {TRACE_STATUS_SCOPE}."


def _marker_warnings(report):
    return [w for w in report.warnings if "recorded task markers" in w]


def _memo_line(nt):
    return [
        line for line in build_incident_memo(nt, DIGEST).splitlines()
        if line.startswith("- **Recorded Task Markers**")
    ]


def _finished():
    saver = InMemorySaver()
    app = _graph({"a": _append("a")}, [("__start__", "a"), ("a", "__end__")]).compile(
        checkpointer=saver
    )
    app.invoke({"log": []}, CONFIG)
    return _ingest(saver, app)


def _paused():
    saver = InMemorySaver()
    app = _graph(
        {"a": _append("a"), "ask": _asks("ask")},
        [("__start__", "a"), ("a", "ask"), ("ask", "__end__")],
    ).compile(checkpointer=saver)
    assert app.invoke({"log": []}, CONFIG)["__interrupt__"]
    return saver, app


def _recursion_stopped():
    saver = InMemorySaver()
    app = _looping().compile(checkpointer=saver)
    with pytest.raises(GraphRecursionError):
        app.invoke({"n": 0}, {**CONFIG, "recursion_limit": 4})
    return saver, app


def test_paused_run_shows_its_interrupt_record():
    saver, _ = _paused()
    nt = _ingest(saver)

    assert nt.trace.status is StepStatus.OK
    assert _marker_warnings(analyze(nt)) == [_warning("current", "1 interrupted")]
    assert _memo_line(nt) == [f"- **Recorded Task Markers**: 1 interrupted ({TRACE_STATUS_SCOPE})"]


def test_recursion_stop_shows_only_with_the_graph():
    saver, app = _recursion_stopped()

    with_graph = _ingest(saver, app)
    assert with_graph.trace.status is StepStatus.OK
    assert _marker_warnings(analyze(with_graph)) == [_warning("current", "1 scheduled")]
    assert _memo_line(with_graph) == [
        f"- **Recorded Task Markers**: 1 scheduled ({TRACE_STATUS_SCOPE})"
    ]

    without_graph = _ingest(saver)
    assert _marker_warnings(analyze(without_graph)) == []
    assert _memo_line(without_graph) == []


@pytest.mark.parametrize("resume", [False, True], ids=["finished", "resumed-to-completion"])
def test_runs_with_no_record_show_nothing(resume):
    if resume:
        saver, app = _paused()
        app.invoke(Command(resume="yes"), CONFIG)
        nt = _ingest(saver, app)
    else:
        nt = _finished()

    assert _marker_warnings(analyze(nt)) == []
    assert _memo_line(nt) == []
    assert "Recorded Task Markers" not in build_incident_memo(nt, DIGEST)


def test_second_interrupt_shows_as_undecided():
    def twice(state):
        first = _asks("1", result={})(state)
        return {"log": [first, _asks("2")(state)]}

    saver = InMemorySaver()
    app = _graph({"t": twice}, [("__start__", "t"), ("t", "__end__")]).compile(
        checkpointer=saver
    )
    app.invoke({"log": []}, CONFIG)
    assert app.invoke(Command(resume="a"), CONFIG)["__interrupt__"]
    nt = _ingest(saver)

    assert _marker_warnings(analyze(nt)) == [_warning("current", "1 undecided interrupt")]


def test_failure_then_interrupt_keeps_the_failure_and_shows_undecided():
    calls = {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt")
        return _asks("q")(state)

    saver = InMemorySaver()
    app = _graph({"f": flaky}, [("__start__", "f"), ("f", "__end__")]).compile(
        checkpointer=saver
    )
    with pytest.raises(RuntimeError):
        app.invoke({"log": []}, CONFIG)
    assert app.invoke(None, CONFIG)["__interrupt__"]
    nt = _ingest(saver)

    assert nt.trace.status is StepStatus.ERROR
    assert [s.name for s in nt.steps if s.status is StepStatus.ERROR] == ["f"]
    assert _marker_warnings(analyze(nt)) == [_warning("current", "1 undecided interrupt")]


def test_record_outliving_a_revised_graph_is_still_shown_as_a_record():
    saver, _ = _paused()
    revised = _graph({"other": _append("other")}, [("__start__", "other"), ("other", "__end__")])
    assert "__interrupt__" not in revised.compile(checkpointer=saver).invoke(None, CONFIG)

    assert _marker_warnings(analyze(_ingest(saver))) == [_warning("current", "1 interrupted")]


def test_paused_inner_tasks_count_as_one_subgraph_task():
    inner = _fan_out(_asks("x"), _asks("y")).compile()
    saver = InMemorySaver()
    app = _graph({"sub": inner}, [("__start__", "sub"), ("sub", "__end__")]).compile(
        checkpointer=saver
    )
    assert len(app.invoke({"log": []}, CONFIG)["__interrupt__"]) == 2

    assert _marker_warnings(analyze(_ingest(saver))) == [_warning("current", "1 interrupted")]


def test_baseline_markers_are_labelled_and_the_report_keeps_its_schema():
    saver, app = _recursion_stopped()
    report = analyze(_finished(), baseline=_ingest(saver, app))

    assert _marker_warnings(report) == [_warning("baseline", "1 scheduled")]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(json.loads(dumps(report)))
