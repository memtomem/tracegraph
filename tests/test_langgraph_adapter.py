"""Phase 1 end-to-end: real LangGraph SqliteSaver trace -> adapter -> normalized graph."""

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from tiny_agent import run

from tracegraph.adapters import LangGraphCheckpointAdapter
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
        },
        metadata={"step": step, "source": "loop", "parents": parents or {}},
        parent_config=pcfg,
        pending_writes=[],
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
