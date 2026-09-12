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
