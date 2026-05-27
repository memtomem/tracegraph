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


# --- guard: cross-namespace parent must not silently fabricate a root ---

from langgraph.checkpoint.base import CheckpointTuple  # noqa: E402


def _ck(cid: str, ns: str, step: int, parent_id: str | None) -> CheckpointTuple:
    cfg = {"configurable": {"thread_id": "A", "checkpoint_ns": ns, "checkpoint_id": cid}}
    pcfg = (
        {"configurable": {"thread_id": "A", "checkpoint_ns": ns, "checkpoint_id": parent_id}}
        if parent_id
        else None
    )
    return CheckpointTuple(
        config=cfg,
        checkpoint={"id": cid, "ts": "2026-01-01T00:00:00+00:00", "channel_values": {}},
        metadata={"step": step, "source": "loop", "parents": {}},
        parent_config=pcfg,
        pending_writes=[],
    )


class _StubSaver:
    def __init__(self, tuples):
        self._tuples = tuples

    def list(self, config):  # noqa: ARG002 - ignores filtering; returns all
        return iter(self._tuples)


def test_cross_namespace_parent_raises():
    # root-ns checkpoint c1 whose parent c0 is a (filtered-out) non-root checkpoint
    root = _ck("c1", "", 1, "c0")
    sub = _ck("c0", "sub", 0, None)
    with pytest.raises(NotImplementedError, match="cross-namespace"):
        LangGraphCheckpointAdapter(_StubSaver([root, sub])).ingest("A")
