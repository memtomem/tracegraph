"""LangGraph tasks a supplied compiled graph schedules at the head, with no write recorded.

The recursion limit, a killed process and a static breakpoint all leave the tasks they had
scheduled without a single pending write, so the checkpoint alone reads like finished work.
With ``schedule_graph`` the adapter asks LangGraph's ``get_state`` which tasks the graph
schedules at the newest root checkpoint and reports each one that has no write there. The
acceptance rule these tests pin: for the graph that wrote the thread, a run that finished, was
resumed, or was forked past to completion reports nothing, and without a graph nothing changes.

Whether a task finished, failed or paused is read from the head's own writes, never from the
snapshot, so ``get_state``'s known quirks (LangGraph 1.2.2 reports a node at its second
``interrupt()`` as finished) cannot produce a step. The drift workflow runs this file against
the newest LangGraph too.
"""

from itertools import pairwise
import copy
import hashlib
import json
import operator
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import CachePolicy, Command, Send, interrupt
from pydantic import BaseModel, field_validator
import pytest
from typer.testing import CliRunner

from tracegraph import artifact
from tracegraph import cli as cli_mod
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.cli import app as cli_app
from tracegraph.graph_ref import fingerprint, load_graph
from tracegraph.model import EdgeOrigin, EdgeType, Step, StepKind, StepSource, StepStatus
from tracegraph.normalize import normalize

CONFIG = {"configurable": {"thread_id": "t"}}
NULL_TASK = "00000000-0000-0000-0000-000000000000"
runner = CliRunner()


@pytest.fixture(params=["memory", "sqlite"])
def saver(request):
    if request.param == "memory":
        yield InMemorySaver()
    else:
        with SqliteSaver.from_conn_string(":memory:") as s:
            yield s


class _State(TypedDict, total=False):
    log: Annotated[list, operator.add]
    n: int


def _append(name):
    def body(state):
        return {"log": [name]}

    return body


def _linear(*names):
    graph = StateGraph(_State)
    for name in names:
        graph.add_node(name, _append(name))
    graph.add_edge(START, names[0])
    for src, dst in pairwise(names):
        graph.add_edge(src, dst)
    graph.add_edge(names[-1], END)
    return graph


def _sends(count):
    graph = StateGraph(_State)
    graph.add_node("w", _append("w"))
    graph.add_conditional_edges(START, lambda state: [Send("w", {"n": i}) for i in range(count)])
    graph.add_edge("w", END)
    return graph


def _ingest(saver, graph):
    return LangGraphCheckpointAdapter(saver, schedule_graph=graph).ingest("t")


def _scheduled(raw):
    return sorted(s.name for s in raw.steps if s.source is StepSource.SCHEDULED)


def _head(raw):
    root = [s for s in raw.steps if "#" not in s.step_id and ":" not in s.step_id]
    return max(root, key=lambda s: s.step_id)


# --- the acceptance rows: finished, resumed or forked-to-completion runs report nothing ---


def _completed_linear(saver):
    app = _linear("a", "b").compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    return app


def _completed_fan_in(saver, defer=False):
    graph = StateGraph(_State)
    for name in "xyj":
        graph.add_node(name, _append(name), **({"defer": True} if defer and name == "j" else {}))
    graph.add_edge(START, "x")
    graph.add_edge(START, "y")
    graph.add_edge(["x", "y"], "j")
    graph.add_edge("j", END)
    app = graph.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    return app


def _completed_reserved_keys(saver):
    """User state keys spelled exactly like LangGraph's trigger channels."""
    reserved = TypedDict(
        "reserved",
        {"log": Annotated[list, operator.add], "branch:to:b": int, "join:p+q:b": object},
        total=False,
    )
    graph = StateGraph(reserved)
    graph.add_node("a", lambda state: {"log": ["a"], "branch:to:b": 1, "join:p+q:b": None})
    graph.add_node("b", _append("b"))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    app = graph.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    return app


def _completed_repeated_subgraph(saver):
    class Inner(TypedDict, total=False):
        log: Annotated[list, operator.add]

    inner = StateGraph(Inner)
    inner.add_node("i1", _append("i1"))
    inner.add_node("i2", _append("i2"))
    inner.add_edge(START, "i1")
    inner.add_edge("i1", "i2")
    inner.add_edge("i2", END)
    outer = StateGraph(_State)
    outer.add_node("w", inner.compile())
    outer.add_conditional_edges(START, lambda s: [Send("w", {"log": [1]}), Send("w", {"log": [2]})])
    outer.add_edge("w", END)
    app = outer.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    return app


def _completed_after_revision(saver):
    _linear("a", "b").compile(checkpointer=saver).invoke({"log": []}, CONFIG)
    revised = _linear("a", "b", "c").compile(checkpointer=saver)
    revised.invoke({"log": []}, CONFIG)
    return revised


def _completed_then_updated(saver):
    app = _completed_linear(saver)
    app.update_state(CONFIG, {"n": 5})
    return app


def _completed_then_copied(saver):
    app = _completed_linear(saver)
    app.update_state(CONFIG, None, as_node="__copy__")
    return app


def _breakpoint_cleared_with_end(saver):
    app = _linear("a", "b").compile(checkpointer=saver, interrupt_before=["b"])
    app.invoke({"log": []}, CONFIG)
    app.update_state(CONFIG, None, as_node=END)
    return app


def _fork_run_to_completion(saver):
    app = _completed_linear(saver)
    middle = next(h for h in app.get_state_history(CONFIG) if h.next == ("b",))
    app.invoke(None, app.update_state(middle.config, {"n": 9}))
    return app


def _interrupt_resumed(saver):
    graph = StateGraph(_State)
    graph.add_node("ask", lambda state: {"log": [interrupt("q")]})
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    app = graph.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    app.invoke(Command(resume="yes"), CONFIG)
    return app


@pytest.mark.parametrize(
    "run",
    [
        _completed_linear,
        _completed_fan_in,
        lambda saver: _completed_fan_in(saver, defer=True),
        _completed_reserved_keys,
        _completed_repeated_subgraph,
        _completed_after_revision,
        _completed_then_updated,
        _completed_then_copied,
        _breakpoint_cleared_with_end,
        _fork_run_to_completion,
        _interrupt_resumed,
    ],
    ids=[
        "linear",
        "fan-in",
        "deferred-barrier",
        "reserved-looking-keys",
        "repeated-subgraph",
        "same-thread-revision",
        "update-state",
        "copy",
        "as-node-end",
        "fork-completed",
        "interrupt-resumed",
    ],
)
def test_finished_runs_report_nothing_but_record_the_graph(saver, run):
    app = run(saver)
    assert app.get_state(CONFIG).next == ()
    raw = _ingest(saver, app)
    assert _scheduled(raw) == []
    assert raw.trace.schedule_graph is not None
    assert raw.trace.schedule_graph.fingerprint == fingerprint(app)
    assert raw.ingest_warnings == []


# --- incomplete runs: one step per scheduled task ---


def _looping():
    graph = StateGraph(_State)
    graph.add_node("a", lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "a")
    graph.add_conditional_edges("a", lambda state: "a" if state["n"] < 100 else END)
    return graph


def test_recursion_limit_reports_the_task_it_stopped_before(saver):
    app = _looping().compile(checkpointer=saver)
    with pytest.raises(GraphRecursionError):
        app.invoke({"n": 0}, {**CONFIG, "recursion_limit": 4})
    raw = _ingest(saver, app)
    assert _scheduled(raw) == ["a"]
    assert raw.trace.status is StepStatus.OK  # trace status still means "did a step error"


@pytest.mark.parametrize(
    "compile_kw, invoke_kw",
    [
        ({"interrupt_before": ["b"]}, {}),
        ({"interrupt_after": ["a"]}, {}),
        ({}, {"interrupt_before": ["b"]}),
    ],
    ids=["compiled-before", "compiled-after", "per-invoke-before"],
)
def test_static_breakpoints_report_the_held_task(saver, compile_kw, invoke_kw):
    _linear("a", "b").compile(checkpointer=saver, **compile_kw).invoke(
        {"log": []}, CONFIG, **invoke_kw
    )
    # The graph handed to tracegraph need not carry the breakpoint: the schedule is the same.
    raw = _ingest(saver, _linear("a", "b").compile())
    assert _scheduled(raw) == ["b"]


def test_send_packets_are_reported_one_step_each(saver):
    _sends(3).compile(checkpointer=saver, interrupt_before=["w"]).invoke({"log": []}, CONFIG)
    raw = _ingest(saver, _sends(3).compile())
    steps = [s for s in raw.steps if s.source is StepSource.SCHEDULED]
    assert [s.name for s in steps] == ["w", "w", "w"]
    assert len({s.step_id for s in steps}) == 3


class _LegacySendsSaver(InMemorySaver):
    """Hands back the head in the v<4 layout, where pending Sends sat in ``pending_sends``."""

    def _legacy(self, tup):
        if tup is None:
            return None
        cp = copy.deepcopy(tup.checkpoint)
        packets = cp["channel_values"].pop("__pregel_tasks", None)
        cp["channel_versions"].pop("__pregel_tasks", None)
        if packets:
            cp["pending_sends"] = packets
            cp["v"] = 3
        return tup._replace(checkpoint=cp)

    def get_tuple(self, config):
        return self._legacy(super().get_tuple(config))

    def list(self, config, **kwargs):
        return (self._legacy(t) for t in super().list(config, **kwargs))


def test_legacy_pending_sends_are_reported_one_step_each():
    saver = _LegacySendsSaver()
    _sends(3).compile(checkpointer=saver, interrupt_before=["w"]).invoke({"log": []}, CONFIG)
    assert "pending_sends" in saver.get_tuple(CONFIG).checkpoint
    assert _scheduled(_ingest(saver, _sends(3).compile())) == ["w", "w", "w"]


class _TupleTopicSaver(InMemorySaver):
    """Hands back the tasks channel in the older ``(flag, packets)`` Topic encoding."""

    def _tupled(self, tup):
        if tup is None:
            return None
        cp = copy.deepcopy(tup.checkpoint)
        packets = cp["channel_values"].get("__pregel_tasks")
        if packets:
            cp["channel_values"]["__pregel_tasks"] = (False, packets)
        return tup._replace(checkpoint=cp)

    def get_tuple(self, config):
        return self._tupled(super().get_tuple(config))

    def list(self, config, **kwargs):
        return (self._tupled(t) for t in super().list(config, **kwargs))


def test_tuple_encoded_sends_are_reported_and_disclosed():
    saver = _TupleTopicSaver()
    _sends(3).compile(checkpointer=saver, interrupt_before=["w"]).invoke({"log": []}, CONFIG)
    assert isinstance(saver.get_tuple(CONFIG).checkpoint["channel_values"]["__pregel_tasks"], tuple)
    assert _scheduled(_ingest(saver, _sends(3).compile())) == ["w", "w", "w"]
    raw = _ingest(saver, _linear("q").compile())
    # Only this disclosure is asserted: the native-failure path reads the same channel as a
    # flat list and adds its own, separate warning for this encoding.
    [warning] = [w for w in raw.ingest_warnings if "supplied graph does not have" in w]
    assert "3 Send packet(s) address node(s) the supplied graph does not have (w)" in warning


def test_legacy_pending_sends_to_an_unknown_node_are_disclosed():
    saver = _LegacySendsSaver()
    _sends(2).compile(checkpointer=saver, interrupt_before=["w"]).invoke({"log": []}, CONFIG)
    raw = _ingest(saver, _linear("q").compile())
    assert _scheduled(raw) == []
    [warning] = raw.ingest_warnings
    assert "2 Send packet(s) address node(s) the supplied graph does not have (w)" in warning


def test_a_head_between_delta_snapshots_is_read_through_the_saver(saver):
    delta = pytest.importorskip("langgraph.channels.delta", reason="DeltaChannel is beta")

    def merge(state, batches):
        return (state or []) + [item for batch in batches for item in batch]

    class Delta(TypedDict, total=False):
        log: Annotated[list, delta.DeltaChannel(merge, snapshot_frequency=1000)]

    received = []

    def key(state):
        # Called while LangGraph prepares b's input during the adapter's read.
        received.append(list(state["log"]))
        return "b"

    def build(cache_key=None):
        graph = StateGraph(Delta)
        graph.add_node("a", lambda state: {"log": ["a"]})
        graph.add_node(
            "b",
            lambda state: {"log": ["b"]},
            **({"cache_policy": CachePolicy(key_func=cache_key)} if cache_key else {}),
        )
        graph.add_edge(START, "a")
        graph.add_edge("a", "b")
        graph.add_edge("b", END)
        return graph

    build().compile(checkpointer=saver, interrupt_before=["b"]).invoke({"log": ["in"]}, CONFIG)
    assert "log" not in saver.get_tuple(CONFIG).checkpoint["channel_values"]
    assert _scheduled(_ingest(saver, build(key).compile())) == ["b"]
    # The head stores no value for the channel; the adapter's read rebuilt it from history.
    assert received == [["in", "a"]]


def test_scheduled_step_hangs_under_the_head(saver):
    graph = StateGraph(_State)
    graph.add_node("plan", _append("plan"))
    graph.add_node("tools", _append("tools"))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "tools")
    graph.add_edge("tools", END)
    graph.compile(checkpointer=saver, interrupt_before=["tools"]).invoke({"log": []}, CONFIG)
    raw = _ingest(saver, graph.compile())
    head = _head(raw)
    [step] = [s for s in raw.steps if s.source is StepSource.SCHEDULED]
    assert step.step_id.startswith(f"{head.step_id}#scheduled:")
    assert (step.name, step.kind, step.seq, step.ts, step.status, step.error_msg) == (
        "tools",
        StepKind.TOOL,
        head.seq + 1,
        head.ts,
        StepStatus.UNSET,
        None,
    )
    [edge] = [e for e in raw.causal_edges if e.src == step.step_id]
    assert (edge.type, edge.dst, edge.origin) == (
        EdgeType.CAUSED_BY,
        head.step_id,
        EdgeOrigin.CHECKPOINT_PARENT,
    )


def test_a_fork_whose_next_task_never_ran_reports_it(saver):
    app = _completed_linear(saver)
    app.update_state(CONFIG, {"n": 6}, as_node="a")
    assert _scheduled(_ingest(saver, app)) == ["b"]


def test_forking_from_the_middle_reports_the_forks_next_task(saver):
    app = _completed_linear(saver)
    middle = next(h for h in app.get_state_history(CONFIG) if h.next == ("b",))
    app.update_state(middle.config, {"n": 9})
    assert _scheduled(_ingest(saver, app)) == ["b"]


# --- disjoint from the paths that read writes ---


def test_paused_tasks_stay_interrupt_steps_only(saver):
    graph = StateGraph(_State)
    graph.add_node("x", lambda state: {"log": [interrupt("q")]})
    graph.add_node("y", _append("y"))
    graph.add_node("z", _append("z"))
    graph.add_edge(START, "x")
    graph.add_edge(START, "y")
    graph.add_edge(["x", "y"], "z")
    graph.add_edge("z", END)
    app = graph.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    raw = _ingest(saver, app)
    assert _scheduled(raw) == []
    assert [s.name for s in raw.steps if s.source is StepSource.INTERRUPT] == ["x"]


def test_a_node_at_its_second_interrupt_is_not_scheduled(saver):
    """LangGraph 1.2.2's snapshot calls this task finished and 1.2.15's calls it paused; its
    head writes say it ran, so it is never a scheduled step on either."""

    def twice(state):
        return {"log": [interrupt("1"), interrupt("2")]}

    graph = StateGraph(_State)
    graph.add_node("t", twice)
    graph.add_edge(START, "t")
    graph.add_edge("t", END)
    app = graph.compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    app.invoke(Command(resume="r1"), CONFIG)
    assert _scheduled(_ingest(saver, app)) == []


def test_without_a_graph_nothing_changes(saver):
    _linear("a", "b").compile(checkpointer=saver, interrupt_before=["b"]).invoke({"log": []}, CONFIG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    assert _scheduled(raw) == []
    assert raw.trace.schedule_graph is None
    header = json.loads(artifact.dumps(normalize(raw)))["trace"]["trace"]
    assert "schedule_graph" not in header and header["trace_id"] == "t"


# --- what LangGraph's own view drops is disclosed, not reported ---


def test_an_unapplied_goto_input_is_disclosed(saver):
    app = _completed_linear(saver)
    head = saver.get_tuple(CONFIG)
    saver.put_writes(head.config, [("branch:to:a", None)], NULL_TASK)
    raw = _ingest(saver, app)
    assert _scheduled(raw) == []
    [warning] = raw.ingest_warnings
    assert "1 input write(s) (a Command goto) wait to be applied" in warning


def test_an_unapplied_goto_send_is_disclosed(saver):
    app = _completed_linear(saver)
    head = saver.get_tuple(CONFIG)
    saver.put_writes(
        head.config, [("__pregel_tasks", Send("a", {})), ("__pregel_tasks", Send("b", {}))], NULL_TASK
    )
    raw = _ingest(saver, app)
    assert _scheduled(raw) == []
    [warning] = raw.ingest_warnings
    assert "2 input write(s) (a Command goto)" in warning


def test_send_packets_to_a_node_the_graph_lacks_are_disclosed(saver):
    _sends(3).compile(checkpointer=saver, interrupt_before=["w"]).invoke({"log": []}, CONFIG)
    raw = _ingest(saver, _linear("q").compile())
    assert _scheduled(raw) == []
    [warning] = raw.ingest_warnings
    assert "3 Send packet(s) address node(s) the supplied graph does not have (w)" in warning


# --- binding: tracegraph's saver, the head, and nothing of the graph's own ---


def test_a_graph_configured_for_an_old_checkpoint_still_reads_the_head(saver):
    app = _linear("a", "b").compile(checkpointer=saver, interrupt_before=["b"])
    app.invoke({"log": []}, CONFIG)
    held = app.get_state(CONFIG).config
    app.invoke(None, CONFIG)
    user_graph = _linear("a", "b").compile(store=InMemoryStore()).with_config(held)
    assert user_graph.copy(update={"checkpointer": saver}).get_state(CONFIG).next == ("b",)
    assert _scheduled(_ingest(saver, user_graph)) == []


def test_no_node_body_runs_while_reading(saver):
    calls = []

    def b(state):
        calls.append("b")
        return {"log": ["b"]}

    graph = StateGraph(_State)
    graph.add_node("a", _append("a"))
    graph.add_node("b", b)
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    graph.compile(checkpointer=saver, interrupt_before=["b"]).invoke({"log": []}, CONFIG)
    assert _scheduled(_ingest(saver, graph.compile())) == ["b"]
    assert calls == []


class _Strict(BaseModel):
    n: int = 0

    @field_validator("n")
    @classmethod
    def refuse(cls, value):
        if value > 0:
            raise ValueError("state rejected")
        return value


def test_a_graph_callback_that_raises_refuses_the_ingest(saver):
    graph = StateGraph(_Strict)
    graph.add_node("a", lambda state: {"n": state.n + 1})
    graph.add_node("b", lambda state: {})
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    # The thread was written by a graph without the validator; reading it through one that
    # rejects the recorded state makes LangGraph raise while preparing the task.
    writer = StateGraph(_State)
    writer.add_node("a", lambda state: {"n": 1})
    writer.add_node("b", _append("b"))
    writer.add_edge(START, "a")
    writer.add_edge("a", "b")
    writer.add_edge("b", END)
    writer.compile(checkpointer=saver, interrupt_before=["b"]).invoke({"n": 0}, CONFIG)
    with pytest.raises(ValueError, match="(?s)could not read checkpoint .*state rejected"):
        _ingest(saver, graph.compile())


def test_a_callback_calling_sys_exit_refuses_the_ingest(saver):
    def key(state):
        raise SystemExit(3)

    graph = StateGraph(_State)
    graph.add_node("a", _append("a"))
    graph.add_node("b", _append("b"), cache_policy=CachePolicy(key_func=key))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    _linear("a", "b").compile(checkpointer=saver, interrupt_before=["b"]).invoke({"log": []}, CONFIG)
    with pytest.raises(ValueError, match="(?s)could not read checkpoint .*SystemExit: 3"):
        _ingest(saver, graph.compile())


# --- the artifact contract ---


def _held_artifact_text():
    saver = InMemorySaver()
    _linear("a", "b").compile(checkpointer=saver, interrupt_before=["b"]).invoke({"log": []}, CONFIG)
    return artifact.dumps(normalize(_ingest(saver, _linear("a", "b").compile())))


def _checked_artifact_text():
    saver = InMemorySaver()
    app = _completed_linear(saver)
    return artifact.dumps(normalize(_ingest(saver, app)))


@pytest.mark.parametrize("text", [_held_artifact_text, _checked_artifact_text], ids=["scheduled", "header-only"])
def test_a_graph_read_stamps_version_6_and_round_trips(text):
    text = text()
    payload = json.loads(text)
    assert payload["schema_version"] == 6
    assert payload["trace"]["trace"]["schedule_graph"]["fingerprint"].startswith("sha256:")
    assert artifact.dumps(artifact.loads(text)) == text


def test_a_scheduled_step_alone_requires_version_6():
    payload = json.loads(_held_artifact_text())
    del payload["trace"]["trace"]["schedule_graph"]
    payload["schema_version"] = 5
    with pytest.raises(ValueError, match="declares schema_version 5 but its content requires 6"):
        artifact.from_obj(payload)
    payload["schema_version"] = 6
    assert json.loads(artifact.dumps(artifact.from_obj(payload)))["schema_version"] == 6


def test_the_header_alone_requires_version_6():
    payload = json.loads(_checked_artifact_text())
    payload["schema_version"] = 5
    with pytest.raises(ValueError, match="declares schema_version 5 but its content requires 6"):
        artifact.from_obj(payload)


def test_older_reader_refuses_version_6_by_name(monkeypatch):
    text = _checked_artifact_text()
    monkeypatch.setattr(artifact, "_READABLE_VERSIONS", (1, 2, 3, 4, 5))
    with pytest.raises(ValueError, match="unsupported artifact schema_version 6"):
        artifact.loads(text)


def test_a_malformed_fingerprint_is_rejected():
    payload = json.loads(_checked_artifact_text())
    payload["trace"]["trace"]["schedule_graph"]["fingerprint"] = "md5:abc"
    with pytest.raises(ValueError):
        artifact.from_obj(payload)


@pytest.mark.parametrize("text", [_held_artifact_text, _checked_artifact_text], ids=["scheduled", "header-only"])
def test_ladybug_round_trip_is_byte_stable_and_stores_the_header(tmp_path, text):
    pytest.importorskip("ladybug", reason="requires the [cypher] extra")
    from tracegraph.store.ladybug import LadybugStore

    src = tmp_path / "graph-read.json"
    src.write_text(text(), encoding="utf-8")
    out = tmp_path / "out.json"
    store = LadybugStore.load_artifact(src)
    try:
        store.export_artifact(out)
        # Export rebuilds the header from memory, so read the stored column itself too.
        rows = store._conn.execute("MATCH (t:Trace) RETURN t.schedule_graph_json")
        stored = rows.get_next()[0]
    finally:
        store.close()
    assert out.read_bytes() == src.read_bytes()
    assert json.loads(stored) == json.loads(src.read_text())["trace"]["trace"]["schedule_graph"]


# --- the fingerprint ---


_FINGERPRINT_SCRIPT = textwrap.dedent(
    """
    import operator, sys
    from typing import Annotated, TypedDict
    from langgraph.graph import END, START, StateGraph
    from tracegraph.graph_ref import fingerprint

    class S(TypedDict, total=False):
        log: Annotated[list, operator.add]

    g = StateGraph(S)
    for n in "abc":
        g.add_node(n, lambda s: {})
    g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("a", "c"); g.add_edge(["b", "c"], END)
    sys.stdout.write(fingerprint(g.compile()))
    """
)


def test_fingerprint_is_stable_across_hash_seeds():
    digests = {
        subprocess.run(
            [sys.executable, "-c", _FINGERPRINT_SCRIPT],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("1", "2", "3")
    }
    assert len(digests) == 1
    assert next(iter(digests)).startswith("sha256:")


def test_fingerprint_tracks_the_schedule_and_nothing_else():
    base = fingerprint(_linear("a", "b").compile())
    assert fingerprint(_linear("a", "b").compile(interrupt_before=["b"])) == base
    assert fingerprint(_linear("a", "b", "c").compile()) != base
    rewired = _linear("a", "b").compile()
    rewired.nodes["b"].triggers = ["n"]
    assert fingerprint(rewired) != base


# --- loading the graph reference ---


_GRAPH_MODULE = textwrap.dedent(
    """
    from __future__ import annotations

    import dataclasses
    import operator
    from typing import Annotated, TypedDict

    from langgraph.graph import END, START, StateGraph


    class S(TypedDict, total=False):
        log: Annotated[list, operator.add]


    @dataclasses.dataclass
    class D:
        n: int = 0


    def build():
        g = StateGraph(S)
        g.add_node("x", lambda s: {"log": ["x"]})
        g.add_node("y", lambda s: {"log": ["y"]})
        g.add_edge(START, "x"); g.add_edge("x", "y"); g.add_edge("y", END)
        return g


    graph = build().compile()
    dataclass_graph = StateGraph(D)
    dataclass_graph.add_node("d", lambda s: {"n": s.n + 1})
    dataclass_graph.add_edge(START, "d"); dataclass_graph.add_edge("d", END)
    dataclass_graph = dataclass_graph.compile()
    uncompiled = build()
    factory = build
    """
)


@pytest.fixture
def graph_file(tmp_path):
    path = tmp_path / "my_graph.py"
    path.write_text(_GRAPH_MODULE, encoding="utf-8")
    return path


def test_a_file_reference_with_postponed_annotations_loads(graph_file):
    before = list(sys.path)
    graph = load_graph(f"{graph_file}:graph")
    assert set(graph.nodes) == {"__start__", "x", "y"}
    assert set(load_graph(f"{graph_file}:dataclass_graph").nodes) == {"__start__", "d"}
    assert sys.path == before


def test_a_dotted_reference_loads_from_the_current_directory(tmp_path, monkeypatch):
    package = tmp_path / "pkg_scheduled_ref"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "agent.py").write_text(_GRAPH_MODULE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "pkg_scheduled_ref", raising=False)
    monkeypatch.delitem(sys.modules, "pkg_scheduled_ref.agent", raising=False)
    assert set(load_graph("pkg_scheduled_ref.agent:graph").nodes) == {"__start__", "x", "y"}


@pytest.mark.parametrize(
    "suffix, match",
    [
        (":nope", "has no attribute 'nope'"),
        (":uncompiled", "uncompiled StateGraph"),
        (":factory", "is a function, not a compiled LangGraph graph"),
        (":S", "not a compiled LangGraph graph"),
        ("", "must be 'path/to/file.py:attr' or 'pkg.module:attr'"),
        (":not an identifier", "must be"),
    ],
)
def test_a_reference_that_names_no_compiled_graph_is_refused(graph_file, suffix, match):
    with pytest.raises(ValueError, match=match):
        load_graph(f"{graph_file}{suffix}")


@pytest.mark.parametrize(
    "body, match",
    [
        ("raise RuntimeError('boom')", "RuntimeError: boom"),
        ("import sys\nsys.exit(4)", "SystemExit: 4"),
        ("import no_such_module_for_tracegraph", "ModuleNotFoundError"),
        ("def broken(:\n    pass", "SyntaxError"),
    ],
)
def test_a_module_that_fails_to_import_is_refused(tmp_path, body, match):
    path = tmp_path / "bad_graph.py"
    path.write_text(body, encoding="utf-8")
    before = (list(sys.path), set(sys.modules))
    with pytest.raises(ValueError, match=f"cannot load graph .*{match}"):
        load_graph(f"{path}:graph")
    assert (list(sys.path), {m for m in sys.modules if m.startswith("_tracegraph_graph_")}) == (
        before[0],
        {m for m in before[1] if m.startswith("_tracegraph_graph_")},
    )


def test_a_module_getattr_that_exits_is_refused(tmp_path):
    path = tmp_path / "lazy_graph.py"
    path.write_text("def __getattr__(name):\n    raise SystemExit(7)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot load graph .*SystemExit: 7"):
        load_graph(f"{path}:graph")


def test_a_failed_reload_keeps_the_module_an_earlier_load_registered(graph_file):
    graph = load_graph(f"{graph_file}:graph")
    # vars(), not getattr: other loaded graph modules may define a module __getattr__.
    [name] = [m for m, mod in list(sys.modules.items()) if vars(mod).get("graph") is graph]
    graph_file.write_text("raise RuntimeError('bad reload')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad reload"):
        load_graph(f"{graph_file}:graph")
    assert sys.modules[name].graph is graph


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no such file"):
        load_graph(f"{tmp_path / 'absent.py'}:graph")


# --- the CLI: kill, render, refuse ---


_KILLED_CHILD = textwrap.dedent(
    """
    import operator, os, sys, time
    from typing import Annotated, TypedDict
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph


    class S(TypedDict, total=False):
        log: Annotated[list, operator.add]


    def y(state):
        if os.environ.get("TRACEGRAPH_TEST_KILL"):
            time.sleep(0.3)
            os._exit(9)
        return {"log": ["y"]}


    def build():
        g = StateGraph(S)
        g.add_node("x", lambda s: {"log": ["x"]})
        g.add_node("y", y)
        g.add_node("z", lambda s: {"log": ["z"]})
        g.add_edge(START, "x"); g.add_edge(START, "y"); g.add_edge(["x", "y"], "z")
        g.add_edge("z", END)
        return g


    graph = build().compile()

    if __name__ == "__main__":
        with SqliteSaver.from_conn_string(sys.argv[1]) as saver:
            build().compile(checkpointer=saver).invoke(
                {"log": []}, {"configurable": {"thread_id": "k"}}
            )
    """
)


@pytest.fixture
def killed_run(tmp_path):
    """A super-step where x wrote its result and y's process died before writing anything."""
    module = tmp_path / "killed_graph.py"
    module.write_text(_KILLED_CHILD, encoding="utf-8")
    db = tmp_path / "killed.sqlite"
    proc = subprocess.run(
        [sys.executable, str(module), str(db)],
        env={**os.environ, "TRACEGRAPH_TEST_KILL": "1"},
        capture_output=True,
    )
    assert proc.returncode == 9, proc.stderr
    return module, db


def _inspect(monkeypatch, path):
    from rich.console import Console

    monkeypatch.setattr(cli_mod, "console", Console(width=200))
    res = runner.invoke(cli_app, ["inspect", str(path)])
    assert res.exit_code == 0, res.output
    return res.output


def test_cli_ingest_reports_the_task_a_kill_left_unwritten(killed_run, tmp_path, monkeypatch):
    module, db = killed_run
    out = tmp_path / "k.json"
    res = runner.invoke(
        cli_app, ["ingest", "--sqlite", str(db), "-t", "k", "-o", str(out), "--graph", f"{module}:graph"]
    )
    assert res.exit_code == 0, res.output
    nt = artifact.load(out)
    assert [s.name for s in nt.steps if s.source is StepSource.SCHEDULED] == ["y"]
    assert json.loads(out.read_text())["schema_version"] == 6

    text = _inspect(monkeypatch, out)
    assert "y  ◌ scheduled\n" in text
    assert text.rstrip().endswith("0 lossy-projection · 1 scheduled")


#: sha256 of the artifact and the width-200 ``inspect`` output for the committed LangGraph
#: fixture, produced by `eaf3ba9` (the release before ``--graph``). An ingest without a graph
#: must keep producing these bytes.
_FIXTURE_ARTIFACT_SHA256 = "000d30f2acd313b3f73cfec9053583feccad4f8f31a0ffea55407748a0c694f8"
_FIXTURE_INSPECT_SHA256 = "a8f15a00669fd02f33a4ceb27697e3b37e9f3676058f7f50204ec70d83e54960"


def test_cli_ingest_without_graph_keeps_the_fixture_bytes(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "langgraph" / "native-failure.sqlite"
    out = tmp_path / "t.json"
    res = runner.invoke(cli_app, ["ingest", "--sqlite", str(fixture), "-t", "t", "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert hashlib.sha256(out.read_bytes()).hexdigest() == _FIXTURE_ARTIFACT_SHA256
    text = _inspect(monkeypatch, out)
    assert hashlib.sha256(text.encode()).hexdigest() == _FIXTURE_INSPECT_SHA256


def test_cli_ingest_without_graph_is_unchanged(killed_run, tmp_path):
    _, db = killed_run
    out = tmp_path / "k.json"
    res = runner.invoke(cli_app, ["ingest", "--sqlite", str(db), "-t", "k", "-o", str(out)])
    assert res.exit_code == 0, res.output
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == 2
    assert "schedule_graph" not in payload["trace"]["trace"]
    assert payload["trace"]["trace"]["trace_id"] == "k"


def test_scheduled_label_escapes_the_name_and_keeps_the_lossy_suffix():
    step = Step(
        step_id="s",
        trace_id="t",
        seq=3,
        name="[bold]tools",
        source=StepSource.SCHEDULED,
        status=StepStatus.UNSET,
        projection_lossy=True,
    )
    # "[bold]" is a real Rich tag, so an unescaped name would be restyled and lose its text.
    assert cli_mod._label(step) == r"[dim]3: \[bold]tools  ◌ scheduled[/]  ⚠ lossy-projection"


@pytest.mark.parametrize(
    "ref, match",
    [
        ("{module}:nope", "has no attribute 'nope'"),
        ("{tmp}/absent.py:graph", "no such file"),
    ],
)
def test_cli_refuses_a_bad_graph_without_writing(killed_run, tmp_path, ref, match):
    module, db = killed_run
    out = tmp_path / "k.json"
    res = runner.invoke(
        cli_app,
        ["ingest", "--sqlite", str(db), "-t", "k", "-o", str(out),
         "--graph", ref.format(module=module, tmp=tmp_path)],
    )
    assert res.exit_code == 2
    assert match in " ".join(res.output.split()).replace("│ ", "").replace(" │", "")
    assert not out.exists()


def test_cli_refuses_when_the_graph_cannot_read_the_head(killed_run, tmp_path):
    _, db = killed_run
    module = tmp_path / "exits.py"
    module.write_text(
        textwrap.dedent(
            """
            import operator
            from typing import Annotated, TypedDict
            from langgraph.graph import END, START, StateGraph
            from langgraph.types import CachePolicy

            class S(TypedDict, total=False):
                log: Annotated[list, operator.add]

            def key(state):
                raise SystemExit(5)

            g = StateGraph(S)
            g.add_node("x", lambda s: {}); g.add_node("y", lambda s: {}, cache_policy=CachePolicy(key_func=key))
            g.add_node("z", lambda s: {})
            g.add_edge(START, "x"); g.add_edge(START, "y"); g.add_edge(["x", "y"], "z"); g.add_edge("z", END)
            graph = g.compile()
            """
        ),
        encoding="utf-8",
    )
    out = tmp_path / "k.json"
    res = runner.invoke(
        cli_app, ["ingest", "--sqlite", str(db), "-t", "k", "-o", str(out), "--graph", f"{module}:graph"]
    )
    assert res.exit_code == 2
    assert "SystemExit: 5" in " ".join(res.output.split())
    assert not out.exists()


_WITHOUT_LANGGRAPH = textwrap.dedent(
    """
    import sys

    class _Hide:
        def find_spec(self, name, path=None, target=None):
            if name.startswith(("langgraph.graph", "langgraph.pregel", "langgraph.types")):
                raise ModuleNotFoundError(f"No module named {name!r}")
            return None

    sys.meta_path.insert(0, _Hide())
    from tracegraph.cli import app
    app(prog_name="tracegraph")
    """
)


def test_graph_free_paths_work_without_langgraph_installed(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "langgraph" / "native-failure.sqlite"

    def run(*args):
        return subprocess.run(
            [sys.executable, "-c", _WITHOUT_LANGGRAPH, *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={**os.environ, "COLUMNS": "200"},
        )

    assert run("--help").returncode == 0
    ok = run("ingest", "--sqlite", str(fixture), "-t", "t", "-o", "plain.json")
    assert ok.returncode == 0, ok.stderr
    refused = run("ingest", "--sqlite", str(fixture), "-t", "t", "-o", "g.json", "--graph", "g.py:graph")
    assert refused.returncode == 2
    assert "--graph requires langgraph to be installed" in " ".join(refused.stderr.split())
    assert not (tmp_path / "g.json").exists()
