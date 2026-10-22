"""LangGraph tasks paused by ``interrupt()``: recorded, never resumed, never finished.

A node that calls ``interrupt()`` leaves a ``(task_id, "__interrupt__", [...])`` pending write on
the checkpoint that scheduled it and no result. The adapter reports such a task only when that
write is its *only* write on the thread's head checkpoint, because every other combination is
ambiguous in the checkpoint itself. The acceptance rule these tests pin: a run that finished,
was resumed, or was forked past is never reported as paused.

Expectations are checked against what ``invoke`` returned, not against ``get_state``: under
LangGraph 1.2.2, after a node's second ``interrupt()`` its own snapshot reports the task as
finished while ``invoke`` hands back the pending interrupt. The drift workflow runs these tests
against the newest LangGraph too, so a claim that holds for one producer version only is
asserted from the record the head actually carries.
"""

import json
import operator
import threading
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt
import pytest

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import analyze
from tracegraph.model import EdgeOrigin, EdgeType, StepSource, StepStatus
from tracegraph.normalize import normalize

from test_capture_provenance import gap_counts, without_provenance
from test_langgraph_adapter import _StubSaver, _ck

CONFIG = {"configurable": {"thread_id": "t"}}
UNDECIDED = "does not record whether each is still paused"


@pytest.fixture(params=["memory", "sqlite"])
def saver(request):
    if request.param == "memory":
        yield InMemorySaver()
    else:
        with SqliteSaver.from_conn_string(":memory:") as s:
            yield s


class _State(TypedDict, total=False):
    log: Annotated[list, operator.add]


def _append(name):
    def body(state):
        return {"log": [name]}

    return body


def _asks(name, result=None):
    def body(state):
        answer = interrupt(name)
        return {"log": [f"{name}:{answer}"]} if result is None else result

    return body


def _graph(nodes, edges, *, conditional=None):
    graph = StateGraph(_State)
    for name, body in nodes.items():
        graph.add_node(name, body)
    for src, dst in edges:
        graph.add_edge(src, dst)
    for src, route in (conditional or {}).items():
        graph.add_conditional_edges(src, route)
    return graph


def _fan_out(x_body, y_body):
    return _graph(
        {"a": _append("a"), "x": x_body, "y": y_body},
        [(START, "a"), ("a", "x"), ("a", "y"), ("x", END), ("y", END)],
    )


def _ingest(saver):
    return LangGraphCheckpointAdapter(saver).ingest("t")


def _paused(raw):
    return sorted(s.name or "" for s in raw.steps if s.source is StepSource.INTERRUPT)


def _assert_under_head(raw, step):
    """The full derived-step contract: a child of the newest root checkpoint, one seq after it,
    at its timestamp, checked on the adapter's *final* seq (after any global resequencing)."""
    root = [s for s in raw.steps if s.source is not StepSource.INTERRUPT and ":" not in s.step_id]
    head = max(root, key=lambda s: s.step_id)
    assert step.step_id.startswith(f"{head.step_id}#interrupt:")
    assert (step.seq, step.ts, step.status, step.error_msg) == (
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


def _interrupt_ids(app):
    return {task.name: task.interrupts[0].id for task in app.get_state(CONFIG).tasks}


def test_paused_task_is_reported_under_the_head(saver):
    app = _graph(
        {"a": _append("a"), "ask": _asks("ask"), "b": _append("b")},
        [(START, "a"), ("a", "ask"), ("ask", "b"), ("b", END)],
    ).compile(checkpointer=saver)
    assert app.invoke({"log": []}, CONFIG)["__interrupt__"]

    raw = _ingest(saver)
    [step] = [s for s in raw.steps if s.source is StepSource.INTERRUPT]
    assert step.name == "ask"
    _assert_under_head(raw, step)
    # Trace status still means "did a step error"; a pause is not an error.
    assert raw.trace.status is StepStatus.OK
    assert not analyze(normalize(raw)).primary_failures

    app.invoke(Command(resume="yes"), CONFIG)
    assert _paused(_ingest(saver)) == []


def test_completed_runs_report_nothing(saver):
    linear = _graph(
        {"a": _append("a"), "b": _append("b")}, [(START, "a"), ("a", "b"), ("b", END)]
    ).compile(checkpointer=saver)
    linear.invoke({"log": []}, CONFIG)
    assert _paused(_ingest(saver)) == []


def test_only_unresumed_siblings_stay_paused(saver):
    app = _fan_out(_asks("x"), _asks("y")).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    assert _paused(_ingest(saver)) == ["x", "y"]

    ids = _interrupt_ids(app)
    out = app.invoke(Command(resume={ids["x"]: 1}), CONFIG)
    assert [i.value for i in out["__interrupt__"]] == ["y"]
    assert _paused(_ingest(saver)) == ["y"]

    app.invoke(Command(resume={ids["y"]: 1}), CONFIG)
    assert _paused(_ingest(saver)) == []


def test_resumed_task_without_a_result_is_never_reported(saver):
    """x resumes and returns ``{}``. Whether that is recorded depends on the producer:
    LangGraph 1.2.2 leaves only ``__interrupt__`` and ``__resume__`` (the same record a second
    ``interrupt()`` leaves, so the state is unknown and only disclosed); 1.2.15 adds
    ``__no_writes__``, which reads as finished. Either way x is never reported as paused."""
    app = _fan_out(_asks("x", result={}), _asks("y")).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    out = app.invoke(Command(resume={_interrupt_ids(app)["x"]: 1}), CONFIG)
    assert [i.value for i in out["__interrupt__"]] == ["y"]

    raw = _ingest(saver)
    assert _paused(raw) == ["y"]
    finished_marker = any(w[1] == "__no_writes__" for w in saver.get_tuple(CONFIG).pending_writes)
    disclosed = any("1 task(s) recorded an interrupt" in w and UNDECIDED in w for w in raw.ingest_warnings)
    assert disclosed is not finished_marker
    assert ("langgraph.undecided_interrupt" in gap_counts(raw)) is disclosed


def test_second_interrupt_in_one_node_is_disclosed_not_reported(saver):
    def twice(state):
        first = interrupt("1")
        second = interrupt("2")
        return {"log": [first, second]}

    app = _graph({"t": twice}, [(START, "t"), ("t", END)]).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    assert _paused(_ingest(saver)) == ["t"]

    out = app.invoke(Command(resume="a"), CONFIG)
    assert [i.value for i in out["__interrupt__"]] == ["2"], "the run really is paused again"
    raw = _ingest(saver)
    assert _paused(raw) == []
    assert any(UNDECIDED in w for w in raw.ingest_warnings)

    app.invoke(Command(resume="b"), CONFIG)
    raw = _ingest(saver)
    assert _paused(raw) == []
    assert not any(UNDECIDED in w for w in raw.ingest_warnings)


def test_failure_then_interrupt_keeps_the_failure_and_discloses_the_pause(saver):
    """Savers keep no write order, so fail-then-interrupt reads like interrupt-then-fail."""
    calls = {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt")
        return {"log": [interrupt("q")]}

    app = _graph({"f": flaky}, [(START, "f"), ("f", END)]).compile(checkpointer=saver)
    with pytest.raises(RuntimeError):
        app.invoke({"log": []}, CONFIG)
    assert app.invoke(None, CONFIG)["__interrupt__"]

    raw = _ingest(saver)
    assert _paused(raw) == []
    assert [s.name for s in raw.steps if s.status is StepStatus.ERROR] == ["f"]
    assert any(UNDECIDED in w for w in raw.ingest_warnings)


def _signal_on_write(saver, monkeypatch, channel):
    """An event set once the saver has stored a write on ``channel``.

    Set from the saver's own ``put_writes``, so a waiting node never reads the saver while it
    is being written (``InMemorySaver`` takes no lock). ``task_path`` stays in the signature
    because LangGraph checks for it.
    """
    recorded = threading.Event()
    put_writes = saver.put_writes

    def put_and_signal(config, writes, task_id, task_path=""):
        put_writes(config, writes, task_id, task_path)
        if any(name == channel for name, _ in writes):
            recorded.set()

    monkeypatch.setattr(saver, "put_writes", put_and_signal)
    return recorded


def test_interrupt_beside_a_raising_sibling(saver, monkeypatch):
    interrupted = _signal_on_write(saver, monkeypatch, "__interrupt__")

    def boom(state):
        # A sibling that raises first stops x, which then records an error instead of its
        # interrupt, so raise only once the interrupt is stored (#164).
        if not interrupted.wait(30):
            raise AssertionError("x's interrupt was not stored within 30 s")
        raise RuntimeError("boom")

    app = _fan_out(_asks("x"), boom).compile(checkpointer=saver)
    with pytest.raises(RuntimeError, match="^boom"):
        app.invoke({"log": []}, CONFIG)

    raw = _ingest(saver)
    assert _paused(raw) == ["x"]
    [failed] = [s for s in raw.steps if s.status is StepStatus.ERROR]
    assert (failed.name, failed.error_msg) == ("y", "RuntimeError('boom')")
    assert raw.trace.status is StepStatus.ERROR


def test_subgraph_pause_is_reported_as_the_subgraph_node(saver):
    def twice(state):
        first = interrupt("1")
        second = interrupt("2")
        return {"log": [first, second]}

    inner = _graph({"t": twice}, [(START, "t"), ("t", END)]).compile()
    app = _graph(
        {"sub": inner, "b": _append("b")}, [(START, "sub"), ("sub", "b"), ("b", END)]
    ).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    raw = _ingest(saver)
    assert _paused(raw) == ["sub"]
    [step] = [s for s in raw.steps if s.source is StepSource.INTERRUPT]
    _assert_under_head(raw, step)
    normalize(raw)  # subgraph checkpoints included, the derived step still validates

    # The inner second interrupt resumes through the null task id at the root, so the
    # subgraph's own root write stays a lone ``__interrupt__``.
    assert app.invoke(Command(resume="a"), CONFIG)["__interrupt__"]
    assert _paused(_ingest(saver)) == ["sub"]

    app.invoke(Command(resume="b"), CONFIG)
    assert _paused(_ingest(saver)) == []


def test_send_workers_are_reported_per_packet(saver):
    app = _graph(
        {"a": _append("a"), "w": _asks("w")},
        [(START, "a"), ("w", END)],
        conditional={"a": lambda state: [Send("w", {}), Send("w", {})]},
    ).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)

    raw = _ingest(saver)
    assert _paused(raw) == ["w", "w"]
    assert len({s.step_id for s in raw.steps if s.source is StepSource.INTERRUPT}) == 2


def test_forking_past_a_paused_head_ends_the_pause(saver):
    app = _graph(
        {"a": _append("a"), "ask": _asks("ask")}, [(START, "a"), ("a", "ask"), ("ask", END)]
    ).compile(checkpointer=saver)
    app.invoke({"log": []}, CONFIG)
    first = next(h for h in app.get_state_history(CONFIG) if h.next == ("a",))
    app.update_state(first.config, {"log": ["edit"]}, as_node="a")
    assert _paused(_ingest(saver)) == []


def test_static_breakpoint_is_not_reported(saver):
    """``interrupt_before`` leaves no write: a documented limit, not a pause we can see."""
    app = _graph(
        {"a": _append("a"), "b": _append("b")}, [(START, "a"), ("a", "b"), ("b", END)]
    ).compile(checkpointer=saver, interrupt_before=["b"])
    app.invoke({"log": []}, CONFIG)
    assert app.get_state(CONFIG).next == ("b",)
    assert _paused(_ingest(saver)) == []


def test_record_outlives_a_revised_graph_that_wrote_no_checkpoint(saver):
    """The step states what the history records. A revised graph without the node finishes
    without writing a checkpoint, so the unresumed interrupt is still the head's record."""
    first = _graph({"ask": _asks("ask")}, [(START, "ask"), ("ask", END)])
    first.compile(checkpointer=saver).invoke({"log": []}, CONFIG)
    head_before = saver.get_tuple(CONFIG).checkpoint["id"]

    revised = _graph({"other": _append("other")}, [(START, "other"), ("other", END)])
    app = revised.compile(checkpointer=saver)
    assert "__interrupt__" not in app.invoke(None, CONFIG)
    assert saver.get_tuple(CONFIG).checkpoint["id"] == head_before
    assert _paused(_ingest(saver)) == ["ask"]


# --- stub checkpoints: shapes a real saver cannot be asked for on demand ---

CID = "1f1ae6ea-e7d8-6c28-8001-03afbf35e17c"


def test_only_the_newest_root_checkpoint_is_read():
    older = _ck(
        "1f1ae6ea-e7d8-6c28-8000-000000000001",
        "",
        0,
        None,
        pending_writes=[("task-old", "__interrupt__", ["?"])],
    )
    head = _ck(CID, "", 1, "1f1ae6ea-e7d8-6c28-8000-000000000001")
    raw = LangGraphCheckpointAdapter(_StubSaver([head, older])).ingest("A")
    assert _paused(raw) == []


def test_unresolvable_interrupted_task_stays_unnamed():
    head = _ck(CID, "", 1, None, pending_writes=[("task-x", "__interrupt__", ["?"])])
    raw = LangGraphCheckpointAdapter(_StubSaver([head])).ingest("A")
    [step] = [s for s in raw.steps if s.source is StepSource.INTERRUPT]
    assert step.name is None
    assert any("interrupted task task-x could not be attributed" in w for w in raw.ingest_warnings)
    assert gap_counts(raw) == {"langgraph.unattributed_interrupted_task": 1}
    assert step.unknown_fields == ["kind", "name"]


def test_non_hex_checkpoint_id_leaves_the_task_unnamed():
    """A third-party saver may use any string as a checkpoint id. No task id can be hashed from
    it, so naming must fail closed instead of raising during hex decoding."""
    head = _ck("c1", "", 1, None, pending_writes=[("task-x", "__interrupt__", ["?"])])
    raw = LangGraphCheckpointAdapter(_StubSaver([head])).ingest("A")
    [step] = [s for s in raw.steps if s.source is StepSource.INTERRUPT]
    assert (step.step_id, step.name) == ("c1#interrupt:task-x", None)
    assert any("interrupted task task-x could not be attributed" in w for w in raw.ingest_warnings)


def test_graph_level_interrupt_writes_are_not_node_tasks():
    head = _ck(
        CID,
        "",
        1,
        None,
        pending_writes=[("00000000-0000-0000-0000-000000000000", "__interrupt__", ["?"])],
    )
    assert _paused(LangGraphCheckpointAdapter(_StubSaver([head])).ingest("A")) == []


def test_derived_id_colliding_with_a_subgraph_checkpoint_is_rejected():
    head = _ck(CID, "", 1, None, pending_writes=[("task-x", "__interrupt__", ["?"])])
    impostor = _ck("task-x", f"{CID}#interrupt", 0, CID, parent_ns="")
    with pytest.raises(ValueError, match="collides"):
        LangGraphCheckpointAdapter(_StubSaver([head, impostor])).ingest("A")


# --- the artifact contract ---


def _paused_artifact_text():
    saver = InMemorySaver()
    _graph({"ask": _asks("ask")}, [(START, "ask"), ("ask", END)]).compile(
        checkpointer=saver
    ).invoke({"log": []}, CONFIG)
    # As a build without capture provenance writes it; v7 outranks every rule tested here.
    return artifact.dumps(without_provenance(normalize(_ingest(saver))))


def test_interrupt_steps_stamp_schema_version_5_and_round_trip():
    text = _paused_artifact_text()
    assert json.loads(text)["schema_version"] == 5
    assert artifact.dumps(artifact.loads(text)) == text


def test_interrupt_outranks_an_origin_priority_projection(monkeypatch):
    """Version 5 implies the v4 projection rule, so an artifact needing both declares 5."""
    import tracegraph.normalize

    nt = artifact.loads(_paused_artifact_text())
    monkeypatch.setattr(tracegraph.normalize, "requires_origin_priority_projection", lambda nt: True)
    assert json.loads(artifact.dumps(nt))["schema_version"] == 5


def test_runs_without_interrupt_steps_keep_their_version():
    saver = InMemorySaver()
    _graph({"a": _append("a")}, [(START, "a"), ("a", END)]).compile(checkpointer=saver).invoke(
        {"log": []}, CONFIG
    )
    nt = normalize(_ingest(saver))
    assert json.loads(artifact.dumps(without_provenance(nt)))["schema_version"] == 2
    assert json.loads(artifact.dumps(nt))["schema_version"] == 7


def test_lower_envelope_around_interrupt_content_is_rejected():
    payload = json.loads(_paused_artifact_text())
    payload["schema_version"] = 4
    with pytest.raises(ValueError, match="declares schema_version 4 but its content requires 5"):
        artifact.from_obj(payload)


def test_older_reader_refuses_version_5_by_name(monkeypatch):
    text = _paused_artifact_text()
    monkeypatch.setattr(artifact, "_READABLE_VERSIONS", (1, 2, 3, 4))
    with pytest.raises(ValueError, match="unsupported artifact schema_version 5"):
        artifact.loads(text)


def test_ladybug_round_trip_is_byte_stable(tmp_path):
    pytest.importorskip("ladybug", reason="requires the [cypher] extra")
    from tracegraph.store.ladybug import LadybugStore

    src = tmp_path / "paused.json"
    src.write_text(_paused_artifact_text(), encoding="utf-8")
    out = tmp_path / "out.json"
    LadybugStore.load_artifact(src).export_artifact(out)
    assert out.read_bytes() == src.read_bytes()
