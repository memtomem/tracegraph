"""A LangGraph subgraph return is recorded per edge (#150), and only where it was applied (#155).

LangGraph records the parent graph's continuation after a subgraph as a child of the checkpoint
that launched it. The adapter replaces that parent with the subgraph checkpoint the continuation
resumed from: the invocation's last checkpoint written before it, since a resumed subgraph
continues from its namespace's latest checkpoint. That choice is made by the adapter, so the
edge has origin ``checkpoint_subgraph_return`` and the artifact is stamped schema 9. It stays an
explicit cause for ``analyze``, and has a declared parent's rank in the derived tree.

A checkpoint a caller writes from the launching checkpoint (``update_state``, a replay's fork, a
copy) is linked through the subgraph only if its ``versions_seen`` shows it applied the subgraph
node's task. What checkpoints cannot show (an update as the subgraph node itself, time travel
around a ``Send``) is still linked; those documented limitations are pinned here so a change to
them is a decision.
"""

import json
import operator
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from tracegraph import artifact
from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
from tracegraph.analysis.diagnose import analyze
from tracegraph.model import (
    Edge,
    EdgeOrigin,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    Trace,
)
from tracegraph.normalize import normalize

RETURN = EdgeOrigin.CHECKPOINT_SUBGRAPH_RETURN
DECLARED = EdgeOrigin.CHECKPOINT_PARENT
CFG = {"configurable": {"thread_id": "t"}}


class _State(TypedDict):
    log: Annotated[list, operator.add]
    error: str | None


def _log(label, **extra):
    return lambda state: {"log": [label], **extra}


def _ask(state):
    return {"log": [f"answer={interrupt('q')}"]}


def _subgraph(*, i1=None, i2=None):
    sub = StateGraph(_State)
    sub.add_node("i1", i1 or _log("i1"))
    sub.add_node("i2", i2 or _log("i2"))
    sub.add_edge(START, "i1")
    sub.add_edge("i1", "i2")
    sub.add_edge("i2", END)
    return sub.compile()


def _app(w, *, after=None, fan_out=False):
    """``before -> w -> after`` on an InMemorySaver; ``w`` is a subgraph or a node."""
    root = StateGraph(_State)
    root.add_node("before", _log("before"))
    root.add_node("w", w)
    root.add_node("after", after or _log("after"))
    root.add_edge(START, "before")
    if fan_out:
        root.add_conditional_edges(
            "before", lambda state: [Send("w", {"log": []})] * 2, ["w"]
        )
    else:
        root.add_edge("before", "w")
    root.add_edge("w", "after")
    root.add_edge("after", END)
    saver = InMemorySaver()
    return root.compile(checkpointer=saver), saver


def _history(saver):
    return sorted(saver.list(CFG), key=lambda t: t.checkpoint["id"])


def _sid(t):
    return LangGraphCheckpointAdapter._step_id(
        t.config["configurable"].get("checkpoint_ns", ""), t.checkpoint["id"]
    )


def _root(saver, source, step):
    [t] = [
        t for t in _history(saver)
        if not t.config["configurable"].get("checkpoint_ns")
        and t.metadata["source"] == source and t.metadata["step"] == step
    ]
    return t


def _sub(saver):
    return [t for t in _history(saver) if t.config["configurable"].get("checkpoint_ns")]


def _causes(raw, effect):
    """``{cause step_id: origin}`` for one effect step."""
    return {e.dst: e.origin for e in raw.causal_edges if e.src == _sid(effect)}


def _pre_subgraph(saver):
    """The root checkpoint that launched ``w``."""
    return _root(saver, "loop", 1)


# --- which subgraph checkpoint a continuation returns through -----------------------------------


def test_the_continuation_returns_through_the_subgraph_terminal_with_its_own_origin():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    terminal = _sub(saver)[-1]
    assert _causes(raw, _root(saver, "loop", 2)) == {_sid(terminal): RETURN}
    others = [e.origin for e in raw.causal_edges if e.origin is not RETURN]
    assert others and set(others) == {DECLARED}
    assert json.loads(artifact.dumps(normalize(raw)))["schema_version"] == 9


def test_each_invocation_of_a_fan_out_returns_through_its_own_terminal():
    app, saver = _app(_subgraph(), fan_out=True)
    app.invoke({"log": [], "error": None}, CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    terminals = {_sid(t) for t in _sub(saver) if t.checkpoint["channel_values"].get("log", [])[-1:] == ["i2"]}
    assert len(terminals) == 2
    assert _causes(raw, _root(saver, "loop", 2)) == dict.fromkeys(terminals, RETURN)


def test_an_update_of_the_launching_checkpoint_as_another_node_keeps_its_declared_parent():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    launch = _pre_subgraph(saver)
    app.update_state(launch.config, {"log": ["edited"]}, as_node="before")
    app.invoke(None, CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [update] = [t for t in _history(saver) if t.metadata["source"] == "update"]
    assert _causes(raw, update) == {_sid(launch): DECLARED}
    # The run resumed from the update launches its own subgraph and returns through it.
    resumed = _sub(saver)[4:]
    [continuation] = [
        t for t in _history(saver)
        if t.parent_config and t.parent_config["configurable"]["checkpoint_id"] == update.checkpoint["id"]
        and not t.config["configurable"].get("checkpoint_ns")
    ]
    assert _causes(raw, continuation) == {_sid(resumed[-1]): RETURN}


def test_an_update_as_the_subgraph_node_itself_is_the_documented_limitation():
    # It applies the subgraph node's writers with the caller's values, so its `versions_seen` moves
    # exactly as when the subgraph's own output is applied, and metadata does not record `as_node`.
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    app.update_state(_pre_subgraph(saver).config, {"log": ["edited"]}, as_node="w")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [update] = [t for t in _history(saver) if t.metadata["source"] == "update"]
    assert _causes(raw, update) == {_sid(_sub(saver)[-1]): RETURN}


def test_an_update_that_consumes_a_finished_subgraphs_output_returns_through_it():
    # The subgraph finished beside an interrupted sibling; `update_state(..., as_node=END)` on the
    # launching checkpoint applies the subgraph's pending output, error included. The error is the
    # subgraph's, not one the update introduced.
    sub = StateGraph(_State)
    sub.add_node("i", _log("sub", error="sub failed"))
    sub.add_edge(START, "i")
    sub.add_edge("i", END)
    root = StateGraph(_State)
    root.add_node("before", _log("before"))
    root.add_node("w", sub.compile())
    root.add_node("sibling", _ask)
    root.add_edge(START, "before")
    root.add_edge("before", "w")
    root.add_edge("before", "sibling")
    root.add_edge(["w", "sibling"], END)
    saver = InMemorySaver()
    app = root.compile(checkpointer=saver)
    app.invoke({"log": [], "error": None}, CFG)
    app.update_state(app.get_state(CFG).config, None, as_node=END)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [update] = [t for t in _history(saver) if t.metadata["source"] == "update"]
    assert "sub" in update.checkpoint["channel_values"]["log"]
    assert _causes(raw, update) == {_sid(_sub(saver)[-1]): RETURN}
    report = analyze(normalize(raw))
    assert [f.step.name for f in report.primary_failures] == ["i"]


def test_clearing_tasks_while_the_subgraph_is_interrupted_is_the_documented_limitation():
    # `update_state(..., as_node=END)` clears the interrupted subgraph task without output, but it
    # applies that task all the same, so its `versions_seen` moves as if the output were applied.
    sub = StateGraph(_State)
    sub.add_node("i", _ask)
    sub.add_edge(START, "i")
    sub.add_edge("i", END)
    root = StateGraph(_State)
    root.add_node("before", _log("before"))
    root.add_node("w", sub.compile())
    root.add_edge(START, "before")
    root.add_edge("before", "w")
    root.add_edge("w", END)
    saver = InMemorySaver()
    app = root.compile(checkpointer=saver)
    app.invoke({"log": [], "error": None}, CFG)
    app.update_state(app.get_state(CFG).config, None, as_node=END)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [update] = [t for t in _history(saver) if t.metadata["source"] == "update"]
    assert update.checkpoint["channel_values"]["log"] == ["before"]
    assert set(_causes(raw, update).values()) == {RETURN}


def test_a_replay_of_a_plain_pregel_launcher_is_linked_as_before():
    # A node subscribed to a plain channel has a task id this adapter does not recognise as pull,
    # so `versions_seen` is not consulted and the replay's fork is linked as in 0.8.0.
    from langgraph.channels import LastValue
    from langgraph.pregel import NodeBuilder, Pregel

    inner = Pregel(
        nodes={"i": NodeBuilder().subscribe_only("x").do(lambda x: x + 1).write_to("out")},
        channels={"x": LastValue(int), "out": LastValue(int)},
        input_channels="x",
        output_channels="out",
    )
    saver = InMemorySaver()
    app = Pregel(
        nodes={
            "before": NodeBuilder().subscribe_only("x").do(lambda x: x).write_to("y"),
            "w": NodeBuilder().subscribe_only("y").do(inner),
        },
        channels={"x": LastValue(int), "y": LastValue(int)},
        input_channels="x",
        output_channels="y",
        checkpointer=saver,
    )
    app.invoke(1, CFG)
    launch = _root(saver, "loop", 0)
    app.invoke(None, launch.config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    assert set(_causes(raw, fork).values()) == {RETURN}


def test_a_replay_keeps_its_declared_parent_and_returns_through_its_own_subgraph():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    launch = _pre_subgraph(saver)
    app.invoke(None, launch.config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    first, second = _sub(saver)[:4], _sub(saver)[4:]
    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    # The fork copies the launching checkpoint, from before the subgraph ran.
    assert _causes(raw, fork) == {_sid(launch): DECLARED}
    continuations = [
        t for t in _history(saver)
        if not t.config["configurable"].get("checkpoint_ns")
        and t.metadata["source"] == "loop" and t.checkpoint["channel_values"].get("log", [])[-1:] == ["i2"]
    ]
    assert [_causes(raw, c) for c in continuations] == [
        {_sid(first[-1]): RETURN},
        {_sid(second[-1]): RETURN},
    ]


def test_a_copy_of_the_continuation_returns_through_the_subgraph():
    # A `__copy__` fork declares the copied checkpoint's parent, the launching checkpoint, and
    # holds the subgraph's output: a failure resumed from it is still downstream of the subgraph.
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    continuation = _root(saver, "loop", 2)
    app.update_state(continuation.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    assert fork.parent_config["configurable"]["checkpoint_id"] == (
        _pre_subgraph(saver).checkpoint["id"]
    )
    assert _causes(raw, fork) == _causes(raw, continuation) == {_sid(_sub(saver)[-1]): RETURN}


def test_a_copy_of_a_repeated_fan_out_continuation_returns_through_every_invocation():
    # `Send` tasks are push tasks: a second fan-out leaves the subgraph node's `versions_seen` as
    # the first one left it, so that entry is no evidence for them and they are linked as before.
    class Looped(TypedDict):
        log: Annotated[list, operator.add]
        error: str | None
        round: int

    fan = lambda state: [Send("w", {"log": []})] * 2  # noqa: E731
    root = StateGraph(Looped)
    root.add_node("before", lambda state: {"round": 0})
    root.add_node("w", _subgraph())
    root.add_node("gate", lambda state: {"round": state["round"] + 1})
    root.add_edge(START, "before")
    root.add_conditional_edges("before", fan, ["w"])
    root.add_edge("w", "gate")
    root.add_conditional_edges("gate", lambda state: fan(state) if state["round"] < 2 else END, ["w", END])
    saver = InMemorySaver()
    app = root.compile(checkpointer=saver)
    app.invoke({"log": [], "error": None, "round": 0}, CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    second = max(
        (t for t in _history(saver) if not t.config["configurable"].get("checkpoint_ns")
         and len(_causes(raw, t)) == 2),
        key=lambda t: t.checkpoint["id"],
    )
    app.update_state(second.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    returns = _causes(raw, second)
    assert len(returns) == 2 and set(returns.values()) == {RETURN}
    assert _causes(raw, fork) == returns


def test_a_node_calling_its_subgraph_twice_is_judged_by_its_own_task():
    # The second call's namespace carries a counter segment (`w:<task_id>|1`); the task is still
    # the launcher's pull task for `w`, so a copy returns through both calls and a replay neither.
    sub = _subgraph()

    def w(state, config):
        sub.invoke(state, config)
        return sub.invoke(state, config)

    app, saver = _app(w)
    app.invoke({"log": [], "error": None}, CFG)
    continuation = _root(saver, "loop", 2)
    launch = _pre_subgraph(saver)
    app.update_state(continuation.config, None, as_node="__copy__")
    app.invoke(None, launch.config)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    returns = _causes(raw, continuation)
    assert len(returns) == 2 and set(returns.values()) == {RETURN}
    copy, replay = sorted(
        (t for t in _history(saver) if t.metadata["source"] == "fork"),
        key=lambda t: t.checkpoint["id"],
    )
    assert _causes(raw, copy) == returns
    assert _causes(raw, replay) == {_sid(launch): DECLARED}


def test_a_history_missing_the_launcher_is_reported_as_partial():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    launch = _pre_subgraph(saver).checkpoint["id"]
    tuples = [t for t in _history(saver) if t.checkpoint["id"] != launch]

    class Stub:
        def list(self, config):  # noqa: ARG002 - one thread
            return iter(tuples)

    with pytest.raises(ValueError, match="declares missing parent checkpoint .* partial/corrupt"):
        LangGraphCheckpointAdapter(Stub()).ingest("t")


def test_a_loop_continuation_returns_whatever_its_versions_seen_says():
    # The loop applies the tasks of the step it continues from, so its `versions_seen` is never
    # consulted: a saver that did not carry the entry over still gets the return.
    import copy

    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    launch, continuation = _pre_subgraph(saver), _root(saver, "loop", 2)
    tuples = [copy.deepcopy(t) for t in _history(saver)]
    [edited] = [t for t in tuples if t.checkpoint["id"] == continuation.checkpoint["id"]]
    if "w" in launch.checkpoint["versions_seen"]:
        edited.checkpoint["versions_seen"]["w"] = dict(launch.checkpoint["versions_seen"]["w"])
    else:
        del edited.checkpoint["versions_seen"]["w"]
    assert edited.checkpoint["versions_seen"].get("w") == launch.checkpoint["versions_seen"].get("w")

    class Stub:
        def list(self, config):  # noqa: ARG002 - one thread
            return iter(tuples)

    raw = LangGraphCheckpointAdapter(Stub()).ingest("t")
    assert _causes(raw, continuation) == {_sid(_sub(saver)[-1]): RETURN}


def test_an_edit_of_a_launcher_inside_a_subgraph_keeps_its_declared_parent():
    # The launcher is itself in a subgraph namespace (`outer:<task_id>`), so the inner subgraph's
    # namespace is judged by its segment below the launcher's.
    mid = StateGraph(_State)
    mid.add_node("o_before", _log("o_before"))
    mid.add_node("w", _subgraph())
    mid.add_node("o_after", _log("o_after"))
    mid.add_edge(START, "o_before")
    mid.add_edge("o_before", "w")
    mid.add_edge("w", "o_after")
    mid.add_edge("o_after", END)
    app, saver = _app(mid.compile())
    app.invoke({"log": [], "error": None}, CFG)
    outer = [t for t in _history(saver) if t.config["configurable"].get("checkpoint_ns", "").count(":") == 1
             and t.config["configurable"]["checkpoint_ns"].startswith("w:")]
    launch = next(t for t in outer if t.checkpoint["channel_values"].get("log", [])[-1:] == ["o_before"]
                  and t.metadata["source"] == "loop")
    continuation = next(t for t in outer if t.parent_config
                        and t.parent_config["configurable"]["checkpoint_id"] == launch.checkpoint["id"]
                        and t.metadata["source"] == "loop")
    app.update_state(launch.config, {"log": ["edited"]}, as_node="o_before")
    app.update_state(continuation.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [update] = [t for t in _history(saver) if t.metadata["source"] == "update"]
    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    assert _causes(raw, update) == {_sid(launch): DECLARED}
    returns = _causes(raw, continuation)
    assert set(returns.values()) == {RETURN}
    assert _causes(raw, fork) == returns


def test_a_copy_of_a_continuation_resumed_without_persisted_writes_returns_through_the_subgraph():
    # With `durability="exit"` the launching checkpoint never stores the subgraph task's writes, so
    # only `versions_seen` shows that the copy holds the subgraph's output.
    app, saver = _app(_subgraph(i2=_ask))
    app.invoke({"log": [], "error": None}, CFG)
    app.invoke(Command(resume="yes"), CFG, durability="exit")
    finished = [t for t in _history(saver) if not t.config["configurable"].get("checkpoint_ns")][-1]
    app.update_state(finished.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    assert "answer=yes" in fork.checkpoint["channel_values"]["log"]
    assert set(_causes(raw, fork).values()) == {RETURN}


def test_a_copy_of_the_launching_checkpoint_keeps_its_declared_parent():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    launch = _pre_subgraph(saver)
    app.update_state(launch.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [fork] = [t for t in _history(saver) if t.metadata["source"] == "fork"]
    # It copies the launching checkpoint, so it declares that checkpoint's parent.
    assert _causes(raw, fork) == {launch.parent_config["configurable"]["checkpoint_id"]: DECLARED}


def test_a_later_edit_of_a_subgraph_checkpoint_does_not_rewrite_the_earlier_return():
    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    terminal = _sub(saver)[-1]
    older = _sub(saver)[-2]
    app.update_state(older.config, {"log": ["later"]}, as_node="i1")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    assert _causes(raw, _root(saver, "loop", 2)) == {_sid(terminal): RETURN}


def test_a_subgraph_resumed_on_an_edited_branch_returns_through_that_branch():
    app, saver = _app(_subgraph(i2=_ask))
    app.invoke({"log": [], "error": None}, CFG)
    step0 = next(t for t in _sub(saver) if t.metadata["source"] == "loop")
    app.update_state(step0.config, {"log": ["edited"]}, as_node="i1")
    app.invoke(Command(resume="yes"), CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    terminal = _sub(saver)[-1]
    assert terminal.checkpoint["channel_values"]["log"][-2:] == ["edited", "answer=yes"]
    assert _causes(raw, _root(saver, "loop", 2)) == {_sid(terminal): RETURN}


def test_a_human_finishing_an_interrupted_subgraph_is_what_it_returns():
    # i1 fails; the human goes back before it and finishes the subgraph as i2. LangGraph returns
    # the update's state without writing another loop checkpoint, so the abandoned failure is
    # not an ancestor of the parent graph's own failure.
    app, saver = _app(
        _subgraph(i1=_log("i1", error="abandoned"), i2=_ask),
        after=_log("after", error="outer"),
    )
    app.invoke({"log": [], "error": None}, CFG)
    step0 = next(t for t in _sub(saver) if t.metadata["source"] == "loop")
    update = app.update_state(step0.config, {"log": ["human"], "error": None}, as_node="i2")
    app.invoke(None, CFG)
    raw = LangGraphCheckpointAdapter(saver).ingest("t")

    [finished] = [t for t in _sub(saver) if t.config["configurable"]["checkpoint_id"]
                  == update["configurable"]["checkpoint_id"]]
    assert _causes(raw, _root(saver, "loop", 2)) == {_sid(finished): RETURN}
    report = analyze(normalize(raw))
    assert sorted(f.step.name for f in report.primary_failures) == ["after", "i1"]


def test_a_node_editing_its_own_subgraph_while_running_is_the_documented_limitation():
    # Both checkpoints are older than the continuation, and nothing in either says which one the
    # node returned: the later is taken. Pinned so a change here is a decision, not an accident.
    holder = {}
    sub = _subgraph()

    def w(state, config):
        result = sub.invoke(state, config)
        terminal = _sub(holder["saver"])[-1]
        holder["app"].update_state(terminal.config, {"log": ["unused"]}, as_node="i1")
        return result

    holder["app"], holder["saver"] = _app(w)
    holder["app"].invoke({"log": [], "error": None}, CFG)
    raw = LangGraphCheckpointAdapter(holder["saver"]).ingest("t")

    [unused] = [t for t in _sub(holder["saver"]) if t.metadata["source"] == "update"]
    assert _causes(raw, _root(holder["saver"], "loop", 2)) == {_sid(unused): RETURN}


# --- analysis, tree and artifact ----------------------------------------------------------------


def test_a_failure_after_a_failed_subgraph_is_still_propagated():
    def after(state):
        raise RuntimeError("after failed")

    app, saver = _app(_subgraph(i2=_log("i2", error="sub failed")), after=after)
    with pytest.raises(RuntimeError):
        app.invoke({"log": [], "error": None}, CFG)
    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    assert RETURN in {e.origin for e in nt.edges_of(EdgeType.CAUSED_BY)}

    report = analyze(nt)
    assert [f.step.name for f in report.primary_failures] == ["i2"]
    assert [s.name for s in report.propagated_failures] == ["after"]


def _fan_in(origin):
    """``c`` has a return parent ``b`` and an earlier containment parent ``a``."""
    return RawTrace(
        trace=Trace(trace_id="t", source_kind="test", thread_id="A"),
        steps=[
            Step(step_id="r", trace_id="t", seq=0, source=StepSource.INPUT, kind=StepKind.CHAIN),
            Step(step_id="a", trace_id="t", seq=1, kind=StepKind.CHAIN),
            Step(step_id="b", trace_id="t", seq=2, kind=StepKind.CHAIN),
            Step(step_id="c", trace_id="t", seq=3, kind=StepKind.CHAIN),
        ],
        causal_edges=[
            Edge(type=EdgeType.CAUSED_BY, src="a", dst="r", origin=DECLARED),
            Edge(type=EdgeType.CAUSED_BY, src="b", dst="r", origin=DECLARED),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="a", origin=EdgeOrigin.SPAN_PARENT_FALLBACK),
            Edge(type=EdgeType.CAUSED_BY, src="c", dst="b", origin=origin),
        ],
    )


@pytest.mark.parametrize("origin", [RETURN, DECLARED])
def test_a_return_outranks_containment_as_a_declared_parent_does(origin):
    nt = normalize(_fan_in(origin))
    assert [e.dst for e in nt.edges_of(EdgeType.TREE_PARENT) if e.src == "c"] == ["b"]


def test_the_artifact_round_trips_and_older_versions_are_still_stamped_as_before():
    nt = normalize(_fan_in(RETURN))
    text = artifact.dumps(nt)
    assert json.loads(text)["schema_version"] == 9
    assert artifact.dumps(artifact.loads(text)) == text

    legacy = normalize(_fan_in(DECLARED))
    legacy_text = artifact.dumps(legacy)
    assert json.loads(legacy_text)["schema_version"] != 9
    assert artifact.dumps(artifact.loads(legacy_text)) == legacy_text


def test_ladybug_round_trips_the_return_origin(tmp_path):
    pytest.importorskip("ladybug")
    from tracegraph.store.ladybug import LadybugStore

    app, saver = _app(_subgraph())
    app.invoke({"log": [], "error": None}, CFG)
    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    with LadybugStore.from_trace(nt) as store:
        store.export_artifact(tmp_path / "out.json")
    assert (tmp_path / "out.json").read_text() == artifact.dumps(nt)
