"""Phase 1 end-to-end: real LangGraph SqliteSaver trace -> adapter -> normalized graph."""

from operator import add
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from tiny_agent import run

from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.adapters.langgraph_checkpoint import _looks_like_tool_node
from tracegraph.analysis import explain
from tracegraph.model import EdgeType, StepKind, StepStatus
from tracegraph.normalize import normalize
from tracegraph.store import InMemoryStore


@pytest.fixture
def saver():
    """A real checkpointer holding two genuine runs: A (error path) and B (ok path)."""
    with SqliteSaver.from_conn_string(":memory:") as s:
        run(s, "A", "boom-please")
        run(s, "B", "hello")
        yield s


def test_discover_finds_both_threads(saver):
    assert set(LangGraphCheckpointAdapter(saver).discover()) == {"A", "B"}


def test_ingest_reconstructs_linear_causal_chain(saver):
    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("A"))
    caused = nt.edges_of(EdgeType.CAUSED_BY)
    roots = [s for s in nt.steps if not any(e.src == s.step_id for e in caused)]
    # exactly one root, and it is the LangGraph input checkpoint
    assert len(roots) == 1
    assert roots[0].source.value == "input"
    # a single LangGraph thread is a linear chain: one cause per non-root step
    assert len(caused) == len(nt.steps) - 1


def test_error_step_detected_and_named(saver):
    raw = LangGraphCheckpointAdapter(saver).ingest("A")
    errors = [s for s in raw.steps if s.status is StepStatus.ERROR]
    assert len(errors) == 1, "exactly the call_tool checkpoint should be the error step"
    assert errors[0].name == "call_tool"
    assert errors[0].kind is StepKind.TOOL
    assert "tool failed" in (errors[0].error_msg or "")


@pytest.mark.parametrize(
    "name",
    [
        "tool", "tools", "call_tool", "run_tools", "tool_node", "execute tool",
        # camelCase / PascalCase / acronym-prefixed:
        "ToolNode", "myTool", "HTTPToolServer",
        # all-caps must still classify (the old `"tool" in name.lower()` matched these, so
        # dropping them would be a silent regression — see review finding #8/#18):
        "TOOL", "TOOLS", "CALL_TOOL", "TOOL_NODE",
    ],
)
def test_tool_node_names_classify_as_tool(name):
    assert _looks_like_tool_node(name)


@pytest.mark.parametrize(
    "name",
    # All merely *contain* "tool" as a substring — none is a tool node. Misclassifying these
    # would leak a bogus kind=TOOL into pattern queries (the Tier-2 bug this guards). Includes
    # all-caps substring cases ("STOOL", "RETOOL") to prove the all-caps fix didn't over-match.
    [
        "retool", "toolbar", "stool", "footstool", "toolkit_loader", "tooling",
        "planner", "agent", "STOOL", "RETOOL", "TOOLBAR", "",
    ],
)
def test_substring_only_names_do_not_classify_as_tool(name):
    assert not _looks_like_tool_node(name)


def test_explain_walks_from_error_to_input_root(saver):
    store = InMemoryStore.from_raw(LangGraphCheckpointAdapter(saver).ingest("A"))
    err = next(s for s in store.trace().steps if s.status is StepStatus.ERROR)
    result = explain(store, err.step_id)
    assert any(s.source.value == "input" for s in result.chain)
    # a single LangGraph thread has no fan-in, so the tree projection loses nothing
    assert not result.is_lossy


def test_ok_run_has_no_error_and_diverges_structurally(saver):
    adapter = LangGraphCheckpointAdapter(saver)
    a = adapter.ingest("A")
    b = adapter.ingest("B")
    assert all(s.status is StepStatus.OK for s in b.steps)
    # the error path (A, via handle_error) has more checkpoints than the ok path (B)
    assert len(a.steps) > len(b.steps)


def test_ingest_unknown_thread_raises(saver):
    with pytest.raises(KeyError):
        LangGraphCheckpointAdapter(saver).ingest("does-not-exist")


def _keep_error(a: str | None, b: str | None) -> str | None:
    return b or a


class _SubgraphState(TypedDict):
    log: Annotated[list[str], add]
    error: Annotated[str | None, _keep_error]


def _log_node(label: str):
    def node(state: _SubgraphState) -> dict:  # noqa: ARG001 - node shape mirrors LangGraph
        return {"log": [label]}

    return node


def _error_node(state: _SubgraphState) -> dict:  # noqa: ARG001 - node shape mirrors LangGraph
    return {"log": ["error"], "error": "boom"}


def _single_node_subgraph(name: str, node):
    g = StateGraph(_SubgraphState)
    g.add_node(name, node)
    g.add_edge(START, name)
    g.add_edge(name, END)
    return g.compile()


def test_real_nested_subgraph_uses_closest_parent_and_exit_edges():
    inner = _single_node_subgraph("inner_step", _log_node("inner_step"))

    outer = StateGraph(_SubgraphState)
    outer.add_node("outer_before", _log_node("outer_before"))
    outer.add_node("inner", inner)
    outer.add_node("outer_after", _log_node("outer_after"))
    outer.add_edge(START, "outer_before")
    outer.add_edge("outer_before", "inner")
    outer.add_edge("inner", "outer_after")
    outer.add_edge("outer_after", END)

    parent = StateGraph(_SubgraphState)
    parent.add_node("before", _log_node("before"))
    parent.add_node("outer", outer.compile())
    parent.add_node("after", _log_node("after"))
    parent.add_edge(START, "before")
    parent.add_edge("before", "outer")
    parent.add_edge("outer", "after")
    parent.add_edge("after", END)

    with SqliteSaver.from_conn_string(":memory:") as s:
        parent.compile(checkpointer=s).invoke(
            {"log": [], "error": None},
            {"configurable": {"thread_id": "nested"}},
        )
        nt = normalize(LangGraphCheckpointAdapter(s).ingest("nested"))

    steps = nt.steps_by_id()
    caused = nt.edges_of(EdgeType.CAUSED_BY)
    parents = {e.src: [] for e in caused}
    for e in caused:
        parents[e.src].append(e.dst)

    inner_inputs = [
        s for s in nt.steps if "|inner:" in s.step_id and s.source.value == "input"
    ]
    assert len(inner_inputs) == 1
    inner_input = inner_inputs[0]
    assert inner_input.name == "inner"
    assert not inner_input.projection_lossy
    assert [steps[p].name for p in parents[inner_input.step_id]] == ["outer_before"]

    outer_inner_return = [
        s
        for s in nt.steps
        if s.step_id.startswith("outer:")
        and "|inner:" not in s.step_id
        and s.name == "inner"
        and s.source.value == "loop"
    ]
    assert len(outer_inner_return) == 1
    assert [steps[p].name for p in parents[outer_inner_return[0].step_id]] == ["inner_step"]

    root_outer_return = [
        s for s in nt.steps if ":" not in s.step_id and s.name == "outer"
    ]
    assert len(root_outer_return) == 1
    assert [steps[p].name for p in parents[root_outer_return[0].step_id]] == ["outer_after"]


def test_real_parallel_subgraphs_preserve_labels_edges_and_propagated_error():
    parent = StateGraph(_SubgraphState)
    parent.add_node("before", _log_node("before"))
    parent.add_node("sub_a", _single_node_subgraph("step", _log_node("a")))
    parent.add_node("sub_b", _single_node_subgraph("step", _error_node))
    parent.add_node("after", _log_node("after"))
    parent.add_edge(START, "before")
    parent.add_edge("before", "sub_a")
    parent.add_edge("before", "sub_b")
    parent.add_edge(["sub_a", "sub_b"], "after")
    parent.add_edge("after", END)

    with SqliteSaver.from_conn_string(":memory:") as s:
        parent.compile(checkpointer=s).invoke(
            {"log": [], "error": None},
            {"configurable": {"thread_id": "parallel"}},
        )
        nt = normalize(LangGraphCheckpointAdapter(s).ingest("parallel"))

    steps = nt.steps_by_id()
    caused = nt.edges_of(EdgeType.CAUSED_BY)
    parents = {e.src: [] for e in caused}
    for e in caused:
        parents[e.src].append(e.dst)

    subgraph_inputs = {
        s.name: s
        for s in nt.steps
        if s.step_id.startswith("sub_") and s.source.value == "input"
    }
    assert set(subgraph_inputs) == {"sub_a", "sub_b"}

    errors = [s for s in nt.steps if s.status is StepStatus.ERROR]
    assert len(errors) == 1
    assert errors[0].step_id.startswith("sub_b:")
    assert errors[0].name == "step"

    fanin_steps = [s for s in nt.steps if s.projection_lossy and ":" not in s.step_id]
    assert len(fanin_steps) == 1
    fanin_parent_ids = parents[fanin_steps[0].step_id]
    assert {p.split(":", 1)[0] for p in fanin_parent_ids} == {"sub_a", "sub_b"}
    assert [steps[p].name for p in fanin_parent_ids] == ["step", "step"]


# --- cross-namespace/subgraph parentage ---

from langgraph.checkpoint.base import CheckpointTuple  # noqa: E402


def _ck(
    cid: str,
    ns: str,
    step: int,
    parent_id: str | None,
    *,
    parent_ns: str | None = None,
    parents: dict[str, str] | None = None,
    channel_values: dict | None = None,
    channel_versions: dict | None = None,
    versions_seen: dict | None = None,
    pending_writes: list | None = None,
    v: int = 4,
) -> CheckpointTuple:
    cfg = {"configurable": {"thread_id": "A", "checkpoint_ns": ns, "checkpoint_id": cid}}
    pcfg = (
        {
            "configurable": {
                "thread_id": "A",
                "checkpoint_ns": ns if parent_ns is None else parent_ns,
                "checkpoint_id": parent_id,
            }
        }
        if parent_id
        else None
    )
    return CheckpointTuple(
        config=cfg,
        checkpoint={
            "id": cid,
            "ts": "2026-01-01T00:00:00+00:00",
            "channel_values": channel_values or {},
            "channel_versions": channel_versions or {},
            "versions_seen": versions_seen or {},
            "v": v,
        },
        metadata={"step": step, "source": "loop", "parents": parents or {}},
        parent_config=pcfg,
        pending_writes=pending_writes or [],
    )


class _StubSaver:
    def __init__(self, tuples):
        self._tuples = tuples

    def list(self, config):  # noqa: ARG002 - ignores filtering; returns all
        return iter(self._tuples)


def test_cross_namespace_parent_config_ingests_subgraph_checkpoint():
    # root-ns checkpoint c1 whose direct parent is a non-root checkpoint.
    root = _ck("c1", "", 1, "c0", parent_ns="sub")
    sub = _ck("c0", "sub", 0, None)
    raw = LangGraphCheckpointAdapter(_StubSaver([root, sub])).ingest("A")
    assert {s.step_id for s in raw.steps} == {"sub:c0", "c1"}
    assert [(e.src, e.dst) for e in raw.causal_edges] == [("c1", "sub:c0")]

    nt = normalize(raw)
    caused = nt.edges_of(EdgeType.CAUSED_BY)
    roots = [s.step_id for s in nt.steps if not any(e.src == s.step_id for e in caused)]
    assert roots == ["sub:c0"]


def test_metadata_parents_connect_subgraph_namespace_root():
    # Real LangGraph subgraphs put their cross-namespace entry parent in metadata.parents
    # on the subgraph input checkpoint, not in parent_config.
    root_parent = _ck("root0", "", 1, None)
    sub_root = _ck("sub0", "sub", -1, None, parents={"": "root0"})
    raw = LangGraphCheckpointAdapter(_StubSaver([sub_root, root_parent])).ingest("A")

    assert [(e.src, e.dst) for e in raw.causal_edges] == [("sub:sub0", "root0")]
    assert {s.step_id for s in raw.steps} == {"root0", "sub:sub0"}
    assert raw.steps[0].step_id == "root0"
    assert raw.steps[1].step_id == "sub:sub0"
    assert raw.steps[0].seq < raw.steps[1].seq


def test_parent_continuation_uses_subgraph_terminal_cause():
    # Real LangGraph subgraphs resume the parent namespace with parent_config still
    # pointing at the checkpoint that launched the subgraph. In the expanded causal graph
    # that namespace-level shortcut must be replaced with the subgraph terminal checkpoint.
    root_before = _ck(
        "001-root-before",
        "",
        1,
        None,
        channel_values={"branch:to:after": True},
    )
    sub_input = _ck("002-sub-input", "sub", -1, None, parents={"": "001-root-before"})
    sub_step = _ck("003-sub-step", "sub", 0, "002-sub-input")
    sub_done = _ck(
        "004-sub-done",
        "sub",
        1,
        "003-sub-step",
        channel_values={"branch:to:wrong_parent": True, "error": "boom"},
    )
    root_after = _ck(
        "005-root-after",
        "",
        2,
        "001-root-before",
        channel_values={"error": "boom"},
    )

    raw = LangGraphCheckpointAdapter(
        _StubSaver([root_after, sub_done, sub_step, sub_input, root_before])
    ).ingest("A")
    caused = {(e.src, e.dst) for e in raw.causal_edges}

    assert ("005-root-after", "sub:004-sub-done") in caused
    assert ("005-root-after", "001-root-before") not in caused
    assert next(s for s in raw.steps if s.step_id == "005-root-after").name == "after"
    errors = [s.step_id for s in raw.steps if s.status is StepStatus.ERROR]
    assert errors == ["sub:004-sub-done"]
    nt = normalize(raw)
    assert [s.step_id for s in nt.steps] == [
        "001-root-before",
        "sub:002-sub-input",
        "sub:003-sub-step",
        "sub:004-sub-done",
        "005-root-after",
    ]


def test_parent_join_checks_all_subgraph_terminal_error_parents():
    root_before = _ck(
        "001-root-before",
        "",
        1,
        None,
        channel_values={
            "branch:to:sub_a": True,
            "branch:to:sub_b": True,
            "branch:to:join": True,
        },
    )
    sub_a_input = _ck("002-sub-a-input", "sub_a", -1, None, parents={"": "001-root-before"})
    sub_a_done = _ck("003-sub-a-done", "sub_a", 0, "002-sub-a-input")
    sub_b_input = _ck("004-sub-b-input", "sub_b", -1, None, parents={"": "001-root-before"})
    sub_b_done = _ck(
        "005-sub-b-done",
        "sub_b",
        0,
        "004-sub-b-input",
        channel_values={"error": "boom"},
    )
    root_join = _ck(
        "006-root-join",
        "",
        2,
        "001-root-before",
        channel_values={"error": "boom"},
    )

    raw = LangGraphCheckpointAdapter(
        _StubSaver(
            [root_join, sub_b_done, sub_b_input, sub_a_done, sub_a_input, root_before]
        )
    ).ingest("A")
    caused = {(e.src, e.dst) for e in raw.causal_edges}
    steps = {s.step_id: s for s in raw.steps}
    errors = [s.step_id for s in raw.steps if s.status is StepStatus.ERROR]

    assert ("006-root-join", "sub_a:003-sub-a-done") in caused
    assert ("006-root-join", "sub_b:005-sub-b-done") in caused
    assert steps["sub_a:002-sub-a-input"].name == "sub_a"
    assert steps["sub_b:004-sub-b-input"].name == "sub_b"
    assert errors == ["sub_b:005-sub-b-done"]


def test_nested_metadata_parents_keep_only_closest_parent():
    # Nested subgraph metadata carries every ancestor namespace. Only the immediate
    # parent namespace is a direct cause for the nested namespace input.
    root_parent = _ck(
        "001-root-parent",
        "",
        1,
        None,
        channel_values={"branch:to:wrong_parent": True},
    )
    outer_parent = _ck(
        "002-outer-parent",
        "outer",
        1,
        None,
        channel_values={"branch:to:inner": True},
    )
    inner_input = _ck(
        "003-inner-input",
        "outer|inner",
        -1,
        None,
        parents={"": "001-root-parent", "outer": "002-outer-parent"},
    )

    nt = normalize(
        LangGraphCheckpointAdapter(
            _StubSaver([inner_input, outer_parent, root_parent])
        ).ingest("A")
    )
    caused = {(e.src, e.dst) for e in nt.edges_of(EdgeType.CAUSED_BY)}
    inner = nt.steps_by_id()["outer|inner:003-inner-input"]

    assert caused == {("outer|inner:003-inner-input", "outer:002-outer-parent")}
    assert not inner.projection_lossy
    assert inner.name == "inner"


def test_cross_namespace_missing_parent_raises():
    root = _ck("c1", "", 1, "missing", parent_ns="sub")
    with pytest.raises(ValueError, match="missing parent checkpoint"):
        LangGraphCheckpointAdapter(_StubSaver([root])).ingest("A")


def _twice_entered_namespace():
    """A loop that enters the same subgraph namespace twice.

    Each entry has its own terminal, so the parent graph has two continuation edges to
    recover, not one. Pairing a single entry with a single terminal per namespace gets at
    most one of them right and silently drops the other.
    """
    return [
        _ck("001-root-before", "", 1, None, channel_values={"branch:to:mid": True}),
        _ck("002-sub-in", "sub", -1, None, parents={"": "001-root-before"}),
        _ck("003-sub-done", "sub", 0, "002-sub-in"),
        _ck("004-root-mid", "", 2, "001-root-before", channel_values={"branch:to:after": True}),
        _ck("005-sub-in", "sub", -1, None, parents={"": "004-root-mid"}),
        _ck("006-sub-done", "sub", 1, "005-sub-in"),
        _ck("007-root-after", "", 3, "004-root-mid"),
    ]


@pytest.mark.parametrize("newest_first", [True, False])
def test_repeated_namespace_recovers_every_invocations_continuation(newest_first):
    """Both continuations must be recovered, under either enumeration order.

    `saver.list()` pages newest-first, but that is an implementation detail: the result must
    not depend on it. Each root step resumes from the terminal of the invocation *it*
    launched, never from another invocation's terminal.
    """
    checkpoints = _twice_entered_namespace()
    ordered = list(reversed(checkpoints)) if newest_first else checkpoints
    raw = LangGraphCheckpointAdapter(_StubSaver(ordered)).ingest("A")
    caused = {(e.src, e.dst) for e in raw.causal_edges}

    assert ("004-root-mid", "sub:003-sub-done") in caused, sorted(caused)
    assert ("007-root-after", "sub:006-sub-done") in caused, sorted(caused)
    # The namespace-level shortcuts they replace must be gone, and no continuation may
    # point at the other invocation's terminal.
    assert ("004-root-mid", "001-root-before") not in caused
    assert ("007-root-after", "004-root-mid") not in caused
    assert ("007-root-after", "sub:003-sub-done") not in caused
    assert ("004-root-mid", "sub:006-sub-done") not in caused
    normalize(raw)


def test_explicit_return_from_a_nested_subgraph_continues_the_outer_invocation():
    """A parent in a *descendant* namespace is a return, not a new entry.

    `outer` launches `outer|inner`, and `outer:005` resumes by explicitly referencing the
    inner terminal. Counting that as an entry would close the outer invocation at its first
    checkpoint, so the root would resume from before the nested work rather than after it,
    losing the inner execution and the remaining outer work from its ancestry.
    """
    checkpoints = [
        _ck("001-root", "", 1, None, channel_values={"branch:to:outer": True}),
        _ck("002-outer-in", "outer", -1, None, parents={"": "001-root"}),
        _ck("003-inner-in", "outer|inner", -1, None, parents={"outer": "002-outer-in"}),
        _ck("004-inner-done", "outer|inner", 0, "003-inner-in"),
        _ck("005-outer-resume", "outer", 0, "004-inner-done", parent_ns="outer|inner"),
        _ck("006-outer-done", "outer", 1, "005-outer-resume"),
        _ck("007-root-after", "", 2, "001-root"),
    ]
    for ordered in (checkpoints, list(reversed(checkpoints))):
        raw = LangGraphCheckpointAdapter(_StubSaver(ordered)).ingest("A")
        caused = {(e.src, e.dst) for e in raw.causal_edges}
        assert ("007-root-after", "outer:006-outer-done") in caused, sorted(caused)
        assert ("007-root-after", "outer:002-outer-in") not in caused, sorted(caused)
        assert ("007-root-after", "001-root") not in caused, sorted(caused)
        normalize(raw)


# --- repeated failures while the error channel is still set (#47) -----------------------------

import sqlite3  # noqa: E402 - kept with the section that needs it
from decimal import Decimal  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402


class _RetryState(TypedDict):
    error: str | None
    attempts: int
    log: Annotated[list, add]


def _retry_graph(*, clear: bool, same_message: bool, passthrough: bool = False):
    """call_tool fails twice; between attempts prepare_retry clears the key, leaves it, or
    returns the whole state (re-writing the error it holds)."""
    def call_tool(state):
        n = state["attempts"] + 1
        return {"error": "tool failed" if same_message else f"tool failed ({n})", "attempts": n}

    def prepare_retry(state):
        if clear:
            return {"error": None}
        if passthrough:
            return {**state, "log": ["retrying"]}
        return {"log": ["retrying"]}

    g = StateGraph(_RetryState)
    g.add_node("call_tool", call_tool)
    g.add_node("prepare_retry", prepare_retry)
    g.add_edge(START, "call_tool")
    g.add_conditional_edges(
        "call_tool", lambda s: "prepare_retry" if s["attempts"] < 2 else END, ["prepare_retry", END]
    )
    g.add_edge("prepare_retry", "call_tool")
    return g


def _new_saver(kind):
    if kind == "memory":
        return InMemorySaver()
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


def _ingest_run(graph, saver_kind="memory", inputs=({"error": None, "attempts": 0, "log": []},)):
    saver = _new_saver(saver_kind)
    app = graph.compile(checkpointer=saver)
    for value in inputs:
        app.invoke(value, {"configurable": {"thread_id": "t"}})
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    errors = [(s.name, s.error_msg) for s in raw.steps if s.status is StepStatus.ERROR]
    gaps = {g.code: g.count for g in raw.trace.capture.gaps}
    return errors, gaps, raw


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("clear, same_message, n_errors, rewritten", [
    (True, True, 2, None),
    (True, False, 2, None),
    # The key still set: the second attempt is not recorded (the value cannot prove a failure)
    # but disclosed, whatever its message.
    (False, False, 1, 1),
    (False, True, 1, 1),
])
def test_a_retry_that_fails_again_is_recorded_or_disclosed(saver_kind, clear, same_message,
                                                         n_errors, rewritten):
    errors, gaps, raw = _ingest_run(_retry_graph(clear=clear, same_message=same_message), saver_kind)
    assert [name for name, _ in errors] == ["call_tool"] * n_errors
    assert gaps.get("langgraph.rewritten_error_channel") == rewritten
    if rewritten:
        assert any("wrote the error channel while it already held an error" in w
                   for w in raw.ingest_warnings)
        from tracegraph import artifact

        reread = artifact.loads(artifact.dumps(normalize(raw)))
        assert {g.code: g.count for g in reread.trace.capture.gaps} == gaps


@pytest.mark.parametrize("same_message, n_errors, rewritten", [(True, 1, 2), (False, 1, 2)])
def test_a_node_returning_the_whole_state_is_never_a_failure(same_message, n_errors, rewritten):
    errors, gaps, _ = _ingest_run(
        _retry_graph(clear=False, same_message=same_message, passthrough=True)
    )
    assert [name for name, _ in errors] == ["call_tool"] * n_errors
    assert gaps["langgraph.rewritten_error_channel"] == rewritten


def _handled_graph(handler):
    g = StateGraph(_RetryState)
    g.add_node("call_tool", lambda s: {"error": "tool failed", "attempts": 1})
    g.add_node("handler", handler)
    g.add_edge(START, "call_tool")
    g.add_edge("call_tool", "handler")
    g.add_edge("handler", END)
    return g


def test_a_handler_passing_the_state_through_is_disclosed_not_failed():
    errors, gaps, _ = _ingest_run(_handled_graph(lambda s: {**s, "log": ["handled"]}))
    assert errors == [("call_tool", "tool failed")]
    assert gaps["langgraph.rewritten_error_channel"] == 1


def test_a_handler_that_rewords_the_error_is_disclosed_not_failed():
    errors, gaps, _ = _ingest_run(_handled_graph(lambda s: {"error": "handled: " + s["error"]}))
    assert errors == [("call_tool", "tool failed")]
    assert gaps["langgraph.rewritten_error_channel"] == 1


class _ListErrorState(TypedDict):
    error: Annotated[list[str], add]
    log: Annotated[list, add]


def test_a_reducer_that_grows_the_error_is_disclosed_not_failed():
    # A healthy handler returning the whole state doubles a list-reducer error channel: the
    # value changes although nothing failed again.
    g = StateGraph(_ListErrorState)
    g.add_node("call_tool", lambda s: {"error": ["tool failed"]})
    g.add_node("handler", lambda s: dict(s))
    g.add_edge(START, "call_tool")
    g.add_edge("call_tool", "handler")
    g.add_edge("handler", END)
    errors, gaps, _ = _ingest_run(g, inputs=({"error": [], "log": []},))
    assert [name for name, _ in errors] == ["call_tool"]
    assert gaps["langgraph.rewritten_error_channel"] == 1


def test_continuation_input_is_never_a_failure_or_a_rewrite():
    g = StateGraph(_RetryState)
    g.add_node("noop", lambda s: {"log": ["noop"]})
    g.add_edge(START, "noop")
    g.add_edge("noop", END)
    for second in ("A", "B"):
        errors, gaps, _ = _ingest_run(g, inputs=(
            {"error": "A", "attempts": 0, "log": []}, {"error": second, "attempts": 0, "log": []}
        ))
        assert len(errors) == 1  # the first input carried an error, as before
        assert "langgraph.rewritten_error_channel" not in gaps


class _OuterState(TypedDict):
    error: str | None
    log: Annotated[list, add]


class _InnerOut(TypedDict):
    log: Annotated[list, add]


def test_an_outer_error_is_not_a_new_failure_after_a_subgraph_returns():
    inner = StateGraph(_OuterState, output_schema=_InnerOut)
    inner.add_node("inner_fail", lambda s: {"error": "inner-error", "log": ["inner"]})
    inner.add_edge(START, "inner_fail")
    inner.add_edge("inner_fail", END)
    outer = StateGraph(_OuterState)
    outer.add_node("fail_outer", lambda s: {"error": "outer-error"})
    outer.add_node("sub", inner.compile())
    outer.add_node("after", lambda s: {"log": ["after"]})
    outer.add_edge(START, "fail_outer")
    outer.add_edge("fail_outer", "sub")
    outer.add_edge("sub", "after")
    outer.add_edge("after", END)
    _, gaps, raw = _ingest_run(outer, "sqlite", ({"error": None, "log": []},))
    root = [s for s in raw.steps if ":" not in s.step_id]
    # In the outer namespace the error is recorded once, where it was written: the return from
    # the subgraph (whose exit parent holds another namespace's error) is not a new failure.
    assert [(s.name, s.error_msg) for s in root if s.status is StepStatus.ERROR] == [
        ("fail_outer", "outer-error")
    ]
    # Inside the subgraph, the node that wrote another error over the propagated one is
    # disclosed, not recorded; the return to the outer namespace discloses nothing.
    assert not [s for s in raw.steps if s.error_msg == "inner-error"]
    assert gaps["langgraph.rewritten_error_channel"] == 1


def _linear(*values, versions=None):
    cks = []
    for i, value in enumerate(values):
        cks.append(_ck(f"00{i}", "", i, f"00{i - 1}" if i else None,
                       channel_values={"error": value} if value is not None else {},
                       channel_versions=({"error": versions[i]} if versions else None)))
    raw = LangGraphCheckpointAdapter(_StubSaver(cks[::-1])).ingest("A")
    errors = [s.step_id for s in raw.steps if s.status is StepStatus.ERROR]
    gaps = {g.code: g.count for g in raw.trace.capture.gaps}
    return errors, gaps


@pytest.mark.parametrize("falsy, text", [(None, "None"), (False, "False"), (0, "0"), ([], "[]")])
def test_a_falsy_parent_never_suppresses_its_string_rendering(falsy, text):
    errors, _ = _linear("x", falsy, text)
    assert errors[-1:] == ["002"]


def test_values_that_raise_on_comparison_are_compared_by_text():
    errors, gaps = _linear(None, Decimal("sNaN"), Decimal("sNaN"))
    assert errors == ["001"] and gaps == {}


def test_a_value_that_can_be_neither_compared_nor_rendered_counts_as_persisted():
    class Opaque:
        def __bool__(self):
            return True

        def __eq__(self, other):
            raise RuntimeError("no")

        def __str__(self):
            raise RuntimeError("no")

        __hash__ = None

    adapter = LangGraphCheckpointAdapter(_StubSaver([]))
    cp = {"channel_values": {"error": Opaque()}, "channel_versions": {"error": "2"}}
    parent = {"channel_values": {"error": Opaque()}, "channel_versions": {"error": "1"}}
    assert adapter._error_introduced(cp, [parent]) is None
    assert adapter._rewrote_error(cp, [parent]) is True
    del cp["channel_versions"]  # without versions, an incomparable value is the same one
    assert adapter._rewrote_error(cp, [parent]) is False


def test_without_versions_a_new_value_is_disclosed_and_the_same_one_is_not():
    errors, gaps = _linear(None, "A", "B")
    assert errors == ["001"] and gaps == {"langgraph.rewritten_error_channel": 1}
    errors, gaps = _linear(None, "A", "A")
    assert errors == ["001"] and gaps == {}
    errors, gaps = _linear(None, "A", "A", versions=["1", "2", "3"])
    assert errors == ["001"] and gaps == {"langgraph.rewritten_error_channel": 1}


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("second, n_errors, rewritten", [("B", 1, 1), ("A", 1, 1)])
def test_a_fork_copy_is_neither_a_failure_nor_a_rewrite(saver_kind, second, n_errors, rewritten):
    # update_state(..., as_node="__copy__") runs no node: it copies a checkpoint and links the
    # copy to that checkpoint's parent, so comparing it by value would read copied state as a
    # new write.
    g = StateGraph(_RetryState)
    g.add_node("first", lambda s: {"error": "A"})
    g.add_node("second", lambda s: {"error": second})
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)
    saver = _new_saver(saver_kind)
    app = g.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "t"}}
    app.invoke({"error": None, "attempts": 0, "log": []}, cfg)

    def counts():
        raw = LangGraphCheckpointAdapter(saver).ingest("t")
        return (sum(s.status is StepStatus.ERROR for s in raw.steps),
                {g.code: g.count for g in raw.trace.capture.gaps}.get("langgraph.rewritten_error_channel"))

    assert counts() == (n_errors, rewritten)
    app.update_state(app.get_state(cfg).config, None, as_node="__copy__")
    assert counts() == (n_errors, rewritten)



@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("second", ["A", "B"])
def test_a_rewrite_alongside_a_subgraph_is_still_disclosed(saver_kind, second):
    # The checkpoint after second_fail and a parallel subgraph has the subgraph's exit as its
    # expanded causal parent; the rewrite is detected against the channel's own history, the
    # declared same-namespace parent.
    inner = StateGraph(_OuterState, output_schema=_InnerOut)
    inner.add_node("inner_step", lambda s: {"log": ["inner"]})
    inner.add_edge(START, "inner_step")
    inner.add_edge("inner_step", END)
    g = StateGraph(_OuterState)
    g.add_node("first_fail", lambda s: {"error": "A"})
    g.add_node("second_fail", lambda s: {"error": second})
    g.add_node("sub", inner.compile())
    g.add_edge(START, "first_fail")
    g.add_edge("first_fail", "second_fail")
    g.add_edge("first_fail", "sub")
    g.add_edge(["second_fail", "sub"], END)
    errors, gaps, _ = _ingest_run(g, saver_kind, ({"error": None, "log": []},))
    assert [m for _, m in errors if m == "B"] == []  # never recorded
    assert gaps["langgraph.rewritten_error_channel"] == 1  # 0 before: the exit hid the history


# --- an error already upstream of a subgraph is not recorded again (#85) ---

from langgraph.types import Send, interrupt  # noqa: E402


class _NoErrorState(TypedDict):
    log: Annotated[list, add]


def _subgraph(node, *, state=_OuterState, checkpointer=None):
    g = StateGraph(state, output_schema=_InnerOut)
    g.add_node("inner", node)
    g.add_edge(START, "inner")
    g.add_edge("inner", END)
    return g.compile(checkpointer=checkpointer)


def _into(sub, first=lambda s: {"error": "outer"}):
    """`first`, then the subgraph `sub`."""
    g = StateGraph(_OuterState)
    g.add_node("first", first)
    g.add_node("sub", sub)
    g.add_edge(START, "first")
    g.add_edge("first", "sub")
    g.add_edge("sub", END)
    return g


def _nested():
    mid = StateGraph(_OuterState, output_schema=_InnerOut)
    mid.add_node("nested", _subgraph(lambda s: {"log": ["in"]}))
    mid.add_edge(START, "nested")
    mid.add_edge("nested", END)
    return _into(mid.compile())


def _fan_out(outer):
    g = StateGraph(_OuterState)
    g.add_node("first", lambda s: {"error": outer})
    for name in ("a", "b"):
        g.add_node(name, _subgraph(lambda s: {"log": ["in"]}))
        g.add_edge(name, END)
    g.add_edge(START, "first")
    g.add_conditional_edges(
        "first", lambda s: [Send(n, {"error": "payload", "log": []}) for n in ("a", "b")]
    )
    return g


def _errors_by_namespace(raw):
    return [(s.step_id.split(":")[0] if ":" in s.step_id else "", s.name, s.error_msg)
            for s in raw.steps if s.status is StepStatus.ERROR]


_CLEAN = ({"error": None, "log": []},)


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
@pytest.mark.parametrize("graph, inputs, expected, rewritten", [
    pytest.param(lambda: _into(_subgraph(lambda s: {"log": ["in"]})), _CLEAN,
                 [("", "first", "outer")], None, id="propagated into a subgraph"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"log": ["in"]}), lambda s: {"log": ["first"]}),
                 ({"error": "caller", "log": []},), [("", None, "caller")], None,
                 id="caller input carried into a subgraph"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"error": "inner"}), lambda s: {"log": ["first"]}),
                 _CLEAN, [("sub", "inner", "inner")], None, id="a failure inside the subgraph"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"error": "again"})), _CLEAN,
                 [("", "first", "outer")], 1, id="the subgraph writes over the propagated error"),
    pytest.param(_nested, _CLEAN, [("", "first", "outer")], None, id="nested subgraphs"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"log": ["in"]}, state=_NoErrorState)), _CLEAN,
                 [("", "first", "outer")], None, id="subgraph state without the error channel"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"error": None, "log": ["in"]})), _CLEAN,
                 [("", "first", "outer")], None, id="the subgraph clears its own copy"),
    pytest.param(lambda: _into(_subgraph(lambda s: {"log": ["in"]})), _CLEAN * 2,
                 [("", "first", "outer")] * 2, None, id="run twice on one thread"),
    pytest.param(lambda: _fan_out("outer"), _CLEAN, [("", "first", "outer")], None,
                 id="Send into subgraphs below an error"),
    pytest.param(lambda: _fan_out(None), _CLEAN, [("a", None, "payload"), ("b", None, "payload")],
                 None, id="Send payload errors into subgraphs below a clean state"),
])
def test_an_error_upstream_of_a_subgraph_is_recorded_once(saver_kind, graph, inputs, expected,
                                                          rewritten):
    _, gaps, raw = _ingest_run(graph(), saver_kind, inputs)
    assert sorted(_errors_by_namespace(raw), key=str) == sorted(expected, key=str)
    assert gaps.get("langgraph.rewritten_error_channel") == rewritten


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
def test_a_persistent_subgraph_re_entered_below_an_error_records_it_once(saver_kind):
    # checkpointer=True keeps one namespace across invocations: the second entry's input
    # checkpoint names the first invocation's clean last checkpoint as its parent, and the
    # outer checkpoint that launched it only in metadata.parents.
    sub = _subgraph(lambda s: {"log": ["in"]}, checkpointer=True)
    graph = _into(sub, lambda s: {"error": "outer" if "fail" in s["log"] else None})
    _, gaps, raw = _ingest_run(graph, saver_kind, ({"error": None, "log": []},
                                                   {"error": None, "log": ["fail"]}))
    assert _errors_by_namespace(raw) == [("", "first", "outer")]
    assert gaps == {}


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
def test_a_new_input_after_an_interrupted_subgraph_failure_is_still_recorded(saver_kind):
    # The interrupted subgraph's exit holds its own error; the new root input is compared with
    # its declared parent, the clean root state, not with that exit.
    inner = StateGraph(_OuterState, output_schema=_InnerOut)
    inner.add_node("fail_inner", lambda s: {"error": "old-inner"})
    inner.add_node("pause", lambda s: {"log": [interrupt("pause")]})
    inner.add_edge(START, "fail_inner")
    inner.add_edge("fail_inner", "pause")
    inner.add_edge("pause", END)
    g = StateGraph(_OuterState)
    g.add_node("sub", inner.compile())
    g.add_node("done", lambda s: {"log": ["done"]})
    g.add_conditional_edges(START, lambda s: "done" if "skip" in s["log"] else "sub")
    g.add_edge("sub", END)
    g.add_edge("done", END)
    _, _, raw = _ingest_run(g, saver_kind, ({"error": None, "log": []},
                                            {"error": "new-caller", "log": ["skip"]}))
    assert sorted(_errors_by_namespace(raw), key=str) == sorted(
        [("sub", "fail_inner", "old-inner"), ("", None, "new-caller")], key=str)


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
def test_a_rewrite_beside_a_subgraph_without_the_error_channel_is_disclosed(saver_kind):
    # Recorded twice before: the checkpoint after `rewrite` and the subgraph had the subgraph's
    # exit, which has no error channel, as its only error parent. It is not a new failure, and
    # `rewrite` wrote the channel while it was set, so the #47 rule now discloses it.
    g = StateGraph(_OuterState)
    g.add_node("first", lambda s: {"error": "outer"})
    g.add_node("rewrite", lambda s: dict(s))
    g.add_node("sub", _subgraph(lambda s: {"log": ["in"]}, state=_NoErrorState))
    g.add_edge(START, "first")
    g.add_edge("first", "rewrite")
    g.add_edge("first", "sub")
    g.add_edge(["rewrite", "sub"], END)
    _, gaps, raw = _ingest_run(g, saver_kind, _CLEAN)
    assert _errors_by_namespace(raw) == [("", "first", "outer")]
    assert gaps == {"langgraph.rewritten_error_channel": 1}
    assert any("wrote the error channel while it already held an error" in w
               for w in raw.ingest_warnings)
    from tracegraph import artifact
    from tracegraph.analysis import check

    current = artifact.loads(artifact.dumps(normalize(raw)))
    assert {x.code: x.count for x in current.trace.capture.gaps} == gaps
    _, _, base = _ingest_run(_into(_subgraph(lambda s: {"log": ["in"]})), saver_kind, _CLEAN)
    report = check.evaluate(current, normalize(base), check.parse_rules(["failures-not-increased"]))
    assert report.rules[0].outcome == "INSUFFICIENT_EVIDENCE"  # the gap blocks PASS on its side


def _input(cid, ns, parent_id=None, *, parent_ns=None, parents=None):
    t = _ck(cid, ns, -1, parent_id, parent_ns=parent_ns, parents=parents)
    t.metadata["source"] = "input"
    return t


@pytest.mark.parametrize("launched_with, expected", [("boom", []), (None, ["sub:003-start"])])
def test_a_subgraph_start_is_compared_with_the_state_it_was_launched_from(launched_with, expected):
    root = _ck("001-root", "", 1, None, channel_values={"error": launched_with} if launched_with else {})
    sub_input = _input("002-input", "sub", parents={"": "001-root"})
    start = _ck("003-start", "sub", 0, "002-input", parents={"": "001-root"},
                channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(_StubSaver([start, sub_input, root])).ingest("A")
    assert [s.step_id for s in raw.steps if s.status is StepStatus.ERROR] == (
        ["001-root"] if launched_with else []) + expected


def test_a_re_entered_subgraph_start_also_reads_its_launch_parent():
    # parent_config names the namespace's previous invocation; metadata.parents the launcher.
    earlier = _ck("001-sub-done", "sub", 1, None)
    root = _ck("002-root", "", 1, None, channel_values={"error": "boom"})
    sub_input = _input("003-input", "sub", "001-sub-done", parents={"": "002-root"})
    start = _ck("004-start", "sub", 0, "003-input", parents={"": "002-root"},
                channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(_StubSaver([start, sub_input, root, earlier])).ingest("A")
    assert [s.step_id for s in raw.steps if s.status is StepStatus.ERROR] == ["002-root"]


def test_a_legacy_subgraph_input_with_a_cross_namespace_parent_config_is_read_through():
    root = _ck("001-root", "", 1, None, channel_values={"error": "boom"})
    sub_input = _input("002-input", "sub", "001-root", parent_ns="")
    start = _ck("003-start", "sub", 0, "002-input", channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(_StubSaver([start, sub_input, root])).ingest("A")
    assert [s.step_id for s in raw.steps if s.status is StepStatus.ERROR] == ["001-root"]


def test_after_a_subgraph_the_namespace_history_still_holds_the_error():
    # The continuation's causal parent is the subgraph's exit, which lacks the error; its
    # declared parent, the checkpoint before the subgraph, has it.
    root_before = _ck("001-root-before", "", 1, None, channel_values={"error": "boom"})
    sub_input = _input("002-sub-input", "sub", parents={"": "001-root-before"})
    sub_done = _ck("003-sub-done", "sub", 0, "002-sub-input", parents={"": "001-root-before"})
    root_after = _ck("004-root-after", "", 2, "001-root-before", channel_values={"error": "boom"})
    raw = LangGraphCheckpointAdapter(
        _StubSaver([root_after, sub_done, sub_input, root_before])).ingest("A")
    assert ("004-root-after", "sub:003-sub-done") in {(e.src, e.dst) for e in raw.causal_edges}
    assert [s.step_id for s in raw.steps if s.status is StepStatus.ERROR] == ["001-root-before"]


@pytest.mark.parametrize("saver_kind", ["memory", "sqlite"])
def test_a_fork_copy_of_a_subgraph_start_does_not_record_the_error_again(saver_kind):
    # update_state(..., as_node="__copy__") links the copy to the copied checkpoint's parent,
    # here the subgraph's input checkpoint, so it too reads through to the launcher.
    inner = StateGraph(_OuterState, output_schema=_InnerOut)
    inner.add_node("pause", lambda s: {"log": [interrupt("pause")]})
    inner.add_edge(START, "pause")
    inner.add_edge("pause", END)
    saver = _new_saver(saver_kind)
    app = _into(inner.compile()).compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "t"}}
    app.invoke({"error": None, "log": []}, cfg)
    start = next(t for t in saver.list(cfg)
                 if t.config["configurable"]["checkpoint_ns"].startswith("sub")
                 and t.metadata["step"] == 0)
    app.update_state(start.config, None, as_node="__copy__")
    raw = LangGraphCheckpointAdapter(saver).ingest("t")
    assert any(s.source.value == "fork" for s in raw.steps)
    assert _errors_by_namespace(raw) == [("", "first", "outer")]
