"""Native LangGraph task failures: exceptions that never reach ``channel_values``.

A node that raises leaves a ``(task_id, "__error__", repr(exc))`` entry in the checkpoint's
pending writes and no further checkpoint. Reading channels alone therefore reports a crashed
run as an affirmative success, which is the failure mode these tests pin shut.

Every test here drives a *real* graph through a real checkpointer — the stub-checkpoint cases
at the bottom exist only for shapes a real saver cannot produce on demand (a legacy
checkpoint version, a colliding id). A synthetic fixture cannot prove the task-id
reconstruction is right, because getting it wrong is exactly what produces a plausible-looking
fixture.
"""

from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
import pytest

from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import analyze
from tracegraph.model import EdgeType, StepKind, StepSource, StepStatus
from tracegraph.normalize import normalize

SENTINEL = "REVIEW_SYNTHETIC_NODE_FAILURE"


@pytest.fixture(params=["memory", "sqlite"])
def saver(request):
    """Both shipped savers. They persist pending writes differently (SQLite orders them by
    task id and index, memory keeps insertion order), so anything order-dependent fails here.
    """
    if request.param == "memory":
        yield InMemorySaver()
    else:
        with SqliteSaver.from_conn_string(":memory:") as s:
            yield s


class _State(TypedDict):
    x: int


def _linear_graph(tool_body):
    graph = StateGraph(_State)
    graph.add_node("plan", lambda state: {"x": 1})
    graph.add_node("call_tool", tool_body)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "call_tool")
    graph.add_edge("call_tool", END)
    return graph


def _invoke(app, config, payload=None):
    """Run to completion or to the first raised exception, whichever comes first."""
    try:
        app.invoke(payload, config)
    except RuntimeError as exc:
        assert SENTINEL in str(exc)


def _config(thread="t"):
    return {"configurable": {"thread_id": thread}}


def _errors(steps):
    return [s for s in steps if s.status is StepStatus.ERROR]


class _ReplaySaver:
    """Serves checkpoint tuples already read from a saver whose connection has closed.

    An async saver's connection lives only inside its context manager, so the tuples are
    collected there and replayed here. They are the saver's own objects, untouched.
    """

    def __init__(self, tuples):
        self._tuples = tuples

    def list(self, config):  # noqa: ARG002 - the tuples are already scoped to one thread
        return iter(self._tuples)


def test_node_exception_is_attributed_to_the_task_that_raised(saver):
    def call_tool(state):
        raise RuntimeError(SENTINEL)

    app = _linear_graph(call_tool).compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})

    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    errors = _errors(nt.steps)
    assert len(errors) == 1
    failure = errors[0]
    assert failure.name == "call_tool"
    assert failure.kind is StepKind.TOOL
    assert failure.source is StepSource.TASK
    assert SENTINEL in (failure.error_msg or "")
    assert nt.trace.status is StepStatus.ERROR

    # The carrying checkpoint is named after the node that *produced* it. Marking it as the
    # error would report "plan failed" for a call_tool exception, which is the whole reason
    # the failure is a derived child instead.
    causes = [e.dst for e in nt.edges_of(EdgeType.CAUSED_BY) if e.src == failure.step_id]
    assert len(causes) == 1
    carrier = nt.steps_by_id()[causes[0]]
    assert carrier.status is StepStatus.OK
    assert carrier.name == "plan"

    report = analyze(nt)
    assert report.error_count == 1
    assert [item.step.step_id for item in report.primary_failures] == [failure.step_id]


def test_head_would_have_reported_this_run_as_clean(saver):
    """The regression in one assertion: no state channel ever carries this failure."""

    def call_tool(state):
        raise RuntimeError(SENTINEL)

    app = _linear_graph(call_tool).compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})

    tuples = list(saver.list(_config()))
    assert not any(
        "error" in (t.checkpoint.get("channel_values") or {}) for t in tuples
    ), "no error state channel exists; only pending writes carry this failure"
    assert any(
        channel == "__error__" for t in tuples for _, channel, _ in (t.pending_writes or [])
    )


def test_recovered_run_keeps_the_failed_attempt(saver):
    """A retry that succeeds does not erase the exception the caller already saw."""
    attempts = {"n": 0}

    def call_tool(state):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError(SENTINEL)
        return {"x": 2}

    app = _linear_graph(call_tool).compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})
    app.invoke(None, _config())

    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    errors = _errors(nt.steps)
    assert len(errors) == 1
    assert errors[0].name == "call_tool"
    assert nt.trace.status is StepStatus.ERROR
    # Recovery is visible structurally: the retry committed a checkpoint, the failure did not.
    assert any(s.name == "call_tool" and s.status is StepStatus.OK for s in nt.steps)


def test_only_the_latest_persisted_error_survives_per_task(saver):
    """Two failures then success leave one record — attempts are never reconstructed."""
    attempts = {"n": 0}

    def call_tool(state):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError(f"{SENTINEL}-{attempts['n']}")
        return {"x": 3}

    app = _linear_graph(call_tool).compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})
    _invoke(app, _config())
    app.invoke(None, _config())

    errors = _errors(normalize(LangGraphCheckpointAdapter(saver).ingest("t")).steps)
    assert len(errors) == 1
    assert f"{SENTINEL}-2" in (errors[0].error_msg or "")


def test_failed_barrier_triggered_node_is_named(saver):
    """A fan-in node hashes with an implicit ``branch:to:`` channel the checkpoint never holds.

    Naming it therefore cannot work from observed channels alone; it needs the node's
    configured trigger set, which is what makes this case distinct from a linear graph.
    """

    class State(TypedDict):
        log: Annotated[list, lambda a, b: a + b]

    graph = StateGraph(State)
    graph.add_node("x", lambda state: {"log": ["x"]})
    graph.add_node("y", lambda state: {"log": ["y"]})

    def join(state):
        raise RuntimeError(SENTINEL)

    graph.add_node("join", join)
    graph.add_edge(START, "x")
    graph.add_edge(START, "y")
    # One multi-source edge, not two single ones: only this builds the `join:x+y:join`
    # barrier channel. Two separate `add_edge` calls produce ordinary branch triggers and
    # would quietly exercise the linear path instead.
    graph.add_edge(["x", "y"], "join")
    graph.add_edge("join", END)
    app = graph.compile(checkpointer=saver)
    _invoke(app, _config(), {"log": []})

    channels = set()
    for t in saver.list(_config()):
        channels |= set(t.checkpoint.get("channel_versions", {}))
    assert "join:x+y:join" in channels, "this topology must really produce a barrier channel"
    assert "branch:to:join" not in channels, (
        "the implicit trigger the hash needs is absent from the checkpoints, "
        "which is what makes this case different from a linear graph"
    )

    errors = _errors(normalize(LangGraphCheckpointAdapter(saver).ingest("t")).steps)
    assert [e.name for e in errors] == ["join"]


def test_send_task_failure_is_named_per_packet(saver):
    """Two ``Send``s to one node are two tasks; the failing one is named, the other is not."""

    class State(TypedDict):
        log: Annotated[list, lambda a, b: a + b]

    graph = StateGraph(State)

    def worker(state):
        if state.get("log") == ["boom"]:
            raise RuntimeError(SENTINEL)
        return {"log": ["ok"]}

    graph.add_node("worker", worker)
    graph.add_conditional_edges(
        START,
        lambda state: [Send("worker", {"log": ["fine"]}), Send("worker", {"log": ["boom"]})],
        ["worker"],
    )
    graph.add_edge("worker", END)
    app = graph.compile(checkpointer=saver)
    _invoke(app, _config(), {"log": []})

    errors = _errors(normalize(LangGraphCheckpointAdapter(saver).ingest("t")).steps)
    assert [e.name for e in errors] == ["worker"]


@pytest.mark.parametrize("saver_kind", ["memory", "async-sqlite"])
def test_cancelled_sibling_is_not_blamed_for_the_failure(saver_kind):
    """When one task raises, LangGraph cancels its siblings *through the same channel*.

    A cancelled node did not fail — it never got to finish. Reading its ``CancelledError``
    like a real exception reports two failures for one fault and sends an investigation after
    a node that was working fine.
    """
    import asyncio
    from contextlib import asynccontextmanager

    # Cancellation only arises where tasks run concurrently, so this needs an async-capable
    # saver: the sync SqliteSaver refuses async methods outright.
    @asynccontextmanager
    async def open_saver():
        if saver_kind == "memory":
            yield InMemorySaver()
        else:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            async with AsyncSqliteSaver.from_conn_string(":memory:") as s:
                yield s

    class State(TypedDict):
        log: Annotated[list, lambda a, b: a + b]

    async def boom(state):
        await asyncio.sleep(0.01)
        raise RuntimeError(SENTINEL)

    async def healthy(state):
        await asyncio.sleep(30)
        return {"log": ["healthy"]}

    graph = StateGraph(State)
    graph.add_node("boom", boom)
    graph.add_node("healthy", healthy)
    graph.add_edge(START, "boom")
    graph.add_edge(START, "healthy")
    graph.add_edge("boom", END)
    graph.add_edge("healthy", END)

    async def run():
        async with open_saver() as saver:
            app = graph.compile(checkpointer=saver)
            try:
                await app.ainvoke({"log": []}, _config())
            except RuntimeError as exc:
                assert SENTINEL in str(exc)
            return [t async for t in saver.alist(_config())] if saver_kind != "memory" else list(
                saver.list(_config())
            )

    tuples = asyncio.run(run())

    # Both tasks really did record under __error__ — that is what makes this a trap.
    persisted = [
        value
        for t in tuples
        for _, channel, value in (t.pending_writes or [])
        if channel == "__error__"
    ]
    assert any("CancelledError" in str(v) for v in persisted)

    nt = normalize(LangGraphCheckpointAdapter(_ReplaySaver(tuples)).ingest("t"))
    errors = _errors(nt.steps)
    assert [e.name for e in errors] == ["boom"]
    assert SENTINEL in (errors[0].error_msg or "")

    cancelled = [s for s in nt.steps if s.name == "healthy" and s.source is StepSource.TASK]
    assert [s.status for s in cancelled] == [StepStatus.UNSET]
    assert cancelled[0].error_msg is None, "a cancelled task has no failure to report"
    assert analyze(nt).error_count == 1
    assert [f.step.name for f in analyze(nt).primary_failures] == ["boom"]


def test_subgraph_failure_reports_inner_and_outer_independently(saver):
    """Namespace containment is invocation ownership, not exception propagation.

    An outer node that catches the inner failure and raises its own is an independent
    failure, so neither error may be demoted to "propagated" on the strength of the inner
    step merely living inside the outer one's namespace.
    """

    class State(TypedDict):
        x: int

    inner = StateGraph(State)

    def inner_tool(state):
        raise RuntimeError(SENTINEL)

    inner.add_node("inner_tool", inner_tool)
    inner.add_edge(START, "inner_tool")
    inner.add_edge("inner_tool", END)

    outer = StateGraph(State)
    outer.add_node("child", inner.compile())
    outer.add_edge(START, "child")
    outer.add_edge("child", END)
    app = outer.compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})

    nt = normalize(LangGraphCheckpointAdapter(saver).ingest("t"))
    errors = _errors(nt.steps)
    assert len(errors) == 2, "the inner task and the subgraph node both recorded a failure"
    error_ids = {e.step_id for e in errors}
    # No edge joins the two failures: nothing in the checkpoints declares one caused the other.
    assert not [
        e for e in nt.edges_of(EdgeType.CAUSED_BY) if e.src in error_ids and e.dst in error_ids
    ]
    assert len(analyze(nt).primary_failures) == 2


def test_functional_task_failure_is_never_misnamed(saver):
    """An unresolvable task stays unnamed rather than borrowing the node's name.

    A functional ``@task`` inside a node produces a second errored task id under a different
    identity formula. Naming both after the node would report two failures of a node that ran
    once, so the unresolved one keeps ``name=None``.
    """
    from langgraph.func import task

    @task
    def inner_tool(value: int) -> int:
        raise RuntimeError(SENTINEL)

    def outer(state):
        return {"x": inner_tool(state["x"]).result()}

    graph = StateGraph(_State)
    graph.add_node("outer", outer)
    graph.add_edge(START, "outer")
    graph.add_edge("outer", END)
    app = graph.compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})

    errors = _errors(normalize(LangGraphCheckpointAdapter(saver).ingest("t")).steps)
    names = sorted((e.name or "<unnamed>") for e in errors)
    assert names.count("outer") <= 1, "one node that ran once must not produce two named failures"
    assert all(name in {"outer", "<unnamed>"} for name in names)


def test_error_is_recorded_even_on_an_abandoned_branch(saver):
    """Replaying past a failure does not retract it: the exception really was raised."""

    def call_tool(state):
        raise RuntimeError(SENTINEL)

    app = _linear_graph(call_tool).compile(checkpointer=saver)
    _invoke(app, _config(), {"x": 0})
    first = list(saver.list(_config()))[-1].config
    app.update_state(first, {"x": 9})

    errors = _errors(normalize(LangGraphCheckpointAdapter(saver).ingest("t")).steps)
    assert len(errors) == 1
    assert SENTINEL in (errors[0].error_msg or "")


# --- stub checkpoints: shapes a real saver cannot be asked for on demand ---

from hashlib import sha1  # noqa: E402

from test_langgraph_adapter import _StubSaver, _ck  # noqa: E402

CID = "1f1ae6ea-e7d8-6c28-8001-03afbf35e17c"


def _expected_task_id(cid: str, *parts: str, legacy: bool = False) -> str:
    """The upstream formula, spelled out here so the adapter is checked against the spec.

    Mirrors ``_uuid5_str`` / ``_xxhash_str`` in ``langgraph.pregel._algo``. Written out rather
    than imported: importing the private helper the adapter deliberately does not depend on
    would make this test agree with the adapter by construction.
    """
    seed = bytes.fromhex(cid.replace("-", ""))
    joined = b"".join(part.encode() for part in parts)
    if legacy:
        digest = sha1(seed, usedforsecurity=False)
        digest.update(joined)
        hexed = digest.hexdigest()
    else:
        from xxhash import xxh3_128_hexdigest

        hexed = xxh3_128_hexdigest(seed + joined)
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}"


def _error_ck(cid, step, task_id, *, parent=None, channel_values=None, **kw):
    return _ck(
        cid,
        "",
        step,
        parent,
        channel_values=channel_values if channel_values is not None else {"branch:to:worker": True},
        pending_writes=[(task_id, "__error__", f"RuntimeError('{SENTINEL}')")],
        **kw,
    )


def test_unresolvable_task_stays_unnamed():
    """No candidate hashes to this id, so the step carries the error without a name."""
    raw = LangGraphCheckpointAdapter(
        _StubSaver([_error_ck(CID, 1, "ffffffff-0000-0000-0000-000000000000")])
    ).ingest("A")
    errors = _errors(raw.steps)
    assert [e.name for e in errors] == [None]
    assert SENTINEL in (errors[0].error_msg or "")
    assert errors[0].kind is StepKind.CHAIN


def test_error_source_node_write_names_the_task():
    """LangGraph states the node outright when the node has an error handler; trust that."""
    tuples = [
        _ck(
            CID,
            "",
            1,
            None,
            channel_values={"branch:to:worker": True},
            pending_writes=[
                ("unknown-task-id", "__error__", f"RuntimeError('{SENTINEL}')"),
                ("unknown-task-id", "__error_source_node__", "real_node"),
            ],
        )
    ]
    errors = _errors(LangGraphCheckpointAdapter(_StubSaver(tuples)).ingest("A").steps)
    assert [e.name for e in errors] == ["real_node"]


def test_legacy_checkpoint_uses_sha1_task_ids():
    """``v <= 1`` checkpoints hash with SHA-1; using xxh3 there would silently lose the name."""
    task_id = _expected_task_id(
        CID, "worker", "2", "worker", "__pregel_pull", "branch:to:worker", legacy=True
    )
    raw = LangGraphCheckpointAdapter(
        _StubSaver([_error_ck(CID, 1, task_id, v=1)])
    ).ingest("A")
    assert [e.name for e in _errors(raw.steps)] == ["worker"]


def test_task_names_survive_global_resequencing():
    """The hash uses ``metadata.step``, never the step's final ``seq``.

    Declared parentage here contradicts the metadata step numbers, so the adapter renumbers
    the whole thread. Hashing with the renumbered value would break attribution on exactly
    the subgraph-bearing threads that need it most.
    """
    parent = _ck("00000000-0000-6000-8000-000000000001", "", 7, None)
    task_id = _expected_task_id(
        CID, "worker", "3", "worker", "__pregel_pull", "branch:to:worker"
    )
    child = _error_ck(CID, 2, task_id, parent="00000000-0000-6000-8000-000000000001")
    raw = LangGraphCheckpointAdapter(_StubSaver([parent, child])).ingest("A")
    errors = _errors(raw.steps)
    assert [e.name for e in errors] == ["worker"]
    assert errors[0].seq != 3, "the thread really was renumbered"


def test_write_order_does_not_change_derived_ids():
    """Savers order pending writes differently; ids must come from the data, not its order."""
    writes = [
        ("task-a", "__error__", "RuntimeError('first')"),
        ("task-b", "__error__", "RuntimeError('second')"),
    ]
    forward = _ck(CID, "", 1, None, pending_writes=writes)
    reverse = _ck(CID, "", 1, None, pending_writes=list(reversed(writes)))
    ids = [
        sorted(s.step_id for s in _errors(LangGraphCheckpointAdapter(_StubSaver([ck])).ingest("A").steps))
        for ck in (forward, reverse)
    ]
    assert ids[0] == ids[1]


def test_derived_id_colliding_with_a_checkpoint_is_rejected():
    """Checkpoint ids are unrestricted strings, so a collision is possible — and fatal.

    Silently overwriting the entry would delete a real checkpoint from the trace, so this
    raises the module's documented ``ValueError`` instead.
    """
    carrier = _error_ck(CID, 1, "task-x")
    impostor = _ck(f"{CID}#task:task-x", "", 2, None)
    with pytest.raises(ValueError, match="collides"):
        LangGraphCheckpointAdapter(_StubSaver([carrier, impostor])).ingest("A")


def test_graph_level_writes_are_not_node_tasks():
    """Writes under the null task id belong to the graph; they are not a failed node."""
    tuples = [
        _ck(
            CID,
            "",
            1,
            None,
            pending_writes=[("00000000-0000-0000-0000-000000000000", "__resume__", "yes")],
        )
    ]
    raw = LangGraphCheckpointAdapter(_StubSaver(tuples)).ingest("A")
    assert not _errors(raw.steps)


def test_unserialized_exception_is_labelled_not_blank():
    """A legacy envelope revives the exception class as ``None``; "None" would read as text."""
    tuples = [_ck(CID, "", 1, None, pending_writes=[("task-x", "__error__", None)])]
    errors = _errors(LangGraphCheckpointAdapter(_StubSaver(tuples)).ingest("A").steps)
    assert len(errors) == 1
    assert "not serialized" in (errors[0].error_msg or "")


def test_undecodable_send_packets_are_disclosed():
    """Without ``langgraph`` installed, Send packets revive as ``None`` and cannot be named."""
    tuples = [
        _ck(
            CID,
            "",
            1,
            None,
            channel_values={"__pregel_tasks": [None, None]},
            pending_writes=[("task-x", "__error__", f"RuntimeError('{SENTINEL}')")],
        )
    ]
    raw = LangGraphCheckpointAdapter(_StubSaver(tuples)).ingest("A")
    assert [e.name for e in _errors(raw.steps)] == [None]
    assert any("Send packet" in w and "2" in w for w in raw.ingest_warnings)


# --- reading a checkpoint database must not execute what it contains ---


def test_hostile_checkpoint_payload_is_not_invoked(tmp_path):
    """A checkpoint row can name a module and a callable. Reading one must not call it.

    LangGraph's msgpack deserializer revives objects by importing `module` and calling `name`,
    both taken from the stored payload, and by default an unrecognized target is merely logged
    before being invoked. That turns "analyze this trace someone sent me" into code execution
    on the analyst's machine, so `ingest_snapshot` pins an empty allowlist.

    The payload is embedded inside an otherwise *valid* checkpoint, because a malformed row
    fails earlier for unrelated reasons and would let this pass while the door stood open.
    """
    import sqlite3

    import ormsgpack
    from langgraph.checkpoint.serde.jsonplus import EXT_CONSTRUCTOR_POS_ARGS

    marker = tmp_path / "PAYLOAD_EXECUTED"
    assert not marker.exists()
    hostile = ormsgpack.Ext(
        EXT_CONSTRUCTOR_POS_ARGS,
        # Path(...).touch() is not reachable through the constructor, so the observable proof
        # is open(marker, "w"): if the payload is invoked at all, the file appears.
        ormsgpack.packb(["builtins", "open", [str(marker), "w"]]),
    )

    db = tmp_path / "hostile.sqlite"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph = StateGraph(_State)
        graph.add_node("plan", lambda state: {"x": 1})
        graph.add_edge(START, "plan")
        graph.add_edge("plan", END)
        graph.compile(checkpointer=saver).invoke({"x": 0}, _config())
        saver.conn.execute("PRAGMA journal_mode=DELETE")

    # Rewrite one checkpoint's channel_values to carry the payload, keeping the row valid.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT rowid, checkpoint FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        checkpoint = ormsgpack.unpackb(row[1], option=ormsgpack.OPT_NON_STR_KEYS)
        checkpoint["channel_values"] = {"x": hostile}
        conn.execute(
            "UPDATE checkpoints SET checkpoint = ? WHERE rowid = ?",
            (ormsgpack.packb(checkpoint, option=ormsgpack.OPT_NON_STR_KEYS), row[0]),
        )
        conn.commit()

    from tracegraph.sqlite_snapshot import ingest_snapshot

    # Whether ingestion succeeds or raises is not the contract; not executing it is.
    try:
        ingest_snapshot(db, "t", error_channel="error")
    except (ValueError, KeyError, TypeError, AttributeError):
        pass

    assert not marker.exists(), "the checkpoint payload was invoked while reading the database"


def test_inert_serde_refuses_an_unregistered_callable():
    """Pin the mechanism directly, so a serializer swap cannot silently re-open the door."""
    import ormsgpack
    from langgraph.checkpoint.serde.jsonplus import EXT_CONSTRUCTOR_POS_ARGS

    from tracegraph.sqlite_snapshot import _inert_serde

    blob = ormsgpack.packb(
        ormsgpack.Ext(
            EXT_CONSTRUCTOR_POS_ARGS,
            ormsgpack.packb(["subprocess", "run", [["echo", "unreachable"]]]),
        ),
        option=ormsgpack.OPT_NON_STR_KEYS,
    )
    revived = _inert_serde().loads_typed(("msgpack", blob))
    assert not hasattr(revived, "returncode"), "subprocess.run was actually called"
    assert revived == [["echo", "unreachable"]], "payload should come back as inert data"


def test_inert_serde_still_round_trips_ordinary_values():
    """The lockdown must not cost the data the adapter actually reads."""
    import datetime

    from tracegraph.sqlite_snapshot import _inert_serde

    serde = _inert_serde()
    for value in ({"x": 1}, [1, 2, 3], {"when": datetime.datetime(2026, 1, 1)}, {"s", "t"}):
        assert serde.loads_typed(serde.dumps_typed(value)) == value


def test_declared_floor_covers_the_serializer_api_we_depend_on():
    """The hardening in `ingest` is only as real as the version floor that guarantees the API.

    `JsonPlusSerializer(allowed_msgpack_modules=...)` does not exist before
    langgraph-checkpoint 4.1 — 4.0.0 raises `TypeError` — so leaving that dependency
    transitive behind `langgraph-checkpoint-sqlite>=2` would let a resolver pick a version
    where `ingest` raises instead of ingesting, and where nothing blocks a hostile payload.
    """
    import tomllib
    from importlib.metadata import version
    from pathlib import Path

    from packaging.requirements import Requirement
    from packaging.version import Version

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    floors = {
        r.name: min(
            (Version(spec.version) for spec in r.specifier if spec.operator in (">=", "==")),
            default=None,
        )
        for r in (Requirement(d) for d in declared)
    }
    assert floors.get("langgraph-checkpoint") is not None, (
        "langgraph-checkpoint must be declared directly: this package calls its serializer API"
    )
    assert floors["langgraph-checkpoint"] >= Version("4.1")
    # And the environment actually running the suite honours it.
    assert Version(version("langgraph-checkpoint")) >= floors["langgraph-checkpoint"]
