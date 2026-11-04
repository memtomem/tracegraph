"""An error-channel value that cannot be rebuilt on the reading machine is still a failure.

A checkpoint written on Windows stores a ``pathlib.WindowsPath`` as an allowed constructor
call; on macOS or Linux that constructor raises. langgraph-checkpoint before 4.3.0 read such a
value as ``None``, so a failure stored in the error channel vanished: the trace came out ``ok``
with no capture gap (#184). From 4.3.0 the serializer returns the stored payload instead, and
the declared floor guarantees that.

The graph and saver are real; only the stored path is rewritten into the shape a Windows
writer produces, because this machine cannot construct a ``WindowsPath`` to store.
"""

import os
import sqlite3
from pathlib import Path, PureWindowsPath
from typing import Any, TypedDict

import ormsgpack
import pytest
from langgraph.checkpoint.serde.jsonplus import EXT_CONSTRUCTOR_POS_ARGS
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from tracegraph.model import StepStatus
from tracegraph.normalize import normalize
from tracegraph.sqlite_snapshot import ingest_snapshot

# pathlib moved its concrete classes to `pathlib._local` in Python 3.13.
_PATHLIB_MODULES = ("pathlib", "pathlib._local")


class _State(TypedDict, total=False):
    log: str
    error: Any


class _Ext:
    """A msgpack extension kept as bytes, so a stored payload can be rewritten unchanged."""

    def __init__(self, code: int, data: bytes) -> None:
        self.code, self.data = code, data


def _keep(code: int, data: bytes) -> _Ext:
    return _Ext(code, data)


def _encode(obj: Any) -> Any:
    if isinstance(obj, _Ext):
        return ormsgpack.Ext(obj.code, obj.data)
    raise TypeError(type(obj))


_WINDOWS_PATH = _Ext(
    EXT_CONSTRUCTOR_POS_ARGS,
    ormsgpack.packb(
        ["pathlib", "WindowsPath", list(PureWindowsPath(r"C:\data\failed-input.csv").parts)]
    ),
)


def _as_windows_path(obj: Any) -> tuple[Any, int]:
    """Replace every stored posix path with a Windows one; return the value and the count."""
    if isinstance(obj, _Ext):
        call = ormsgpack.unpackb(obj.data, ext_hook=_keep, option=ormsgpack.OPT_NON_STR_KEYS)
        if (
            isinstance(call, list)
            and len(call) >= 2
            and call[0] in _PATHLIB_MODULES
            and call[1] == "PosixPath"
        ):
            return _WINDOWS_PATH, 1
        return obj, 0
    if isinstance(obj, dict):
        out, total = {}, 0
        for key, value in obj.items():
            out[key], n = _as_windows_path(value)
            total += n
        return out, total
    if isinstance(obj, list):
        items = [_as_windows_path(value) for value in obj]
        return [value for value, _ in items], sum(n for _, n in items)
    return obj, 0


def _windows_written_checkpoint(db) -> None:
    graph = StateGraph(_State)
    graph.add_node("plan", lambda state: {"log": "planned"})
    graph.add_node("fetch", lambda state: {"error": Path("/data/failed-input.csv")})
    graph.add_node("report", lambda state: {"log": "reported"})
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "fetch")
    graph.add_edge("fetch", "report")
    graph.add_edge("report", END)
    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph.compile(checkpointer=saver).invoke(
            {"log": "start"}, {"configurable": {"thread_id": "t"}}
        )
        saver.conn.execute("PRAGMA journal_mode=DELETE")

    rewritten = {"writes": 0, "checkpoints": 0}
    with sqlite3.connect(db) as conn:
        for table, column in (("writes", "value"), ("checkpoints", "checkpoint")):
            rows = conn.execute(f"SELECT rowid, type, {column} FROM {table}").fetchall()
            for rowid, kind, blob in rows:
                if kind != "msgpack" or blob is None:
                    continue
                value = ormsgpack.unpackb(blob, ext_hook=_keep, option=ormsgpack.OPT_NON_STR_KEYS)
                value, n = _as_windows_path(value)
                if n:
                    new = ormsgpack.packb(value, default=_encode, option=ormsgpack.OPT_NON_STR_KEYS)
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (new, rowid))
                    rewritten[table] += 1
        conn.commit()
    # The fetch write and the two checkpoints that follow it (after fetch, after report).
    assert rewritten == {"writes": 1, "checkpoints": 2}, rewritten


@pytest.mark.skipif(os.name == "nt", reason="a WindowsPath can be rebuilt on Windows")
def test_error_value_that_cannot_be_rebuilt_is_still_recorded(tmp_path):
    db = tmp_path / "windows-written.sqlite"
    _windows_written_checkpoint(db)

    nt = normalize(ingest_snapshot(db, "t", error_channel="error"))

    failed = [step for step in nt.steps if step.status is StepStatus.ERROR]
    assert [step.name for step in failed] == ["fetch"]
    assert nt.trace.status is StepStatus.ERROR
    # The message is the stored payload, since the value itself cannot be rebuilt here.
    assert "failed-input.csv" in (failed[0].error_msg or "")
