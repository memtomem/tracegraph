"""Analyze checkpoints on a private SQLite backup, without initializing the source.

Reading a checkpoint database is the one place this tool touches a file format that can
*execute*. LangGraph's msgpack deserializer revives objects by importing a module and calling
a name, both taken from the stored payload; by default an unrecognized target only logs a
warning and is then called. A crafted checkpoint row naming ``os.system`` therefore runs a
command on the machine doing the analysis — which is the opposite of what a read-only
forensic tool should do. So this module hands the saver a serializer with an **empty**
allowlist: framework-registered types still revive, and anything else comes back as inert
data instead of being invoked.
"""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any

from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
from tracegraph.input_validation import check_text_depth
from tracegraph.model import RawTrace

# Every JSON text `SqliteSaver` parses while listing one thread: each checkpoint's metadata,
# its body when serialized as JSON, and its JSON-serialized writes. The saver reads writes per
# listed checkpoint, so a write whose checkpoint is missing is never parsed and is not scanned.
_JSON_TEXTS = (
    "SELECT metadata FROM checkpoints WHERE thread_id = ?",
    "SELECT checkpoint FROM checkpoints WHERE thread_id = ? AND type = 'json'",
    """SELECT value FROM writes AS w WHERE w.thread_id = ? AND w.type = 'json' AND EXISTS (
        SELECT 1 FROM checkpoints AS c WHERE c.thread_id = w.thread_id
        AND c.checkpoint_ns = w.checkpoint_ns AND c.checkpoint_id = w.checkpoint_id)""",
)


def _inert_serde():
    """A checkpoint deserializer that revives data but never invokes an arbitrary callable.

    ``allowed_msgpack_modules=()`` keeps the framework's own registered types working — dates,
    sets, `Send` packets, the values the adapter actually reads — while an unregistered
    ``(module, name)`` pair is refused and handed back as its raw arguments. LangGraph offers
    the same lockdown through ``LANGGRAPH_STRICT_MSGPACK``, but an environment variable is not
    a guarantee a library can make about itself.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=())


def _check_json_depth(conn: sqlite3.Connection, thread: str, deadline: float) -> None:
    """Refuse the thread if any JSON text the saver will parse is nested too deep.

    Python 3.12 and 3.13's ``json`` refuse deep nesting and 3.14's parses it, so the text is
    measured here, before the saver decodes it, the same way on every interpreter. Charged to
    the snapshot deadline before each value, once the scan ends, and inside SQLite while a
    query runs. A value found too deep while the deadline passes is reported as too deep:
    either refuses the input, and the nesting is a property of it.
    """
    expired = False

    def interrupt() -> int:
        nonlocal expired
        expired = time.monotonic() >= deadline
        return expired

    def check_deadline() -> None:
        if time.monotonic() >= deadline:
            raise ValueError("SQLite snapshot deadline exceeded")

    conn.set_progress_handler(interrupt, 10_000)
    try:
        for query in _JSON_TEXTS:
            for (text,) in conn.execute(query, (thread,)):
                check_deadline()
                if isinstance(text, (str, bytes)):
                    try:
                        check_text_depth(text)
                    except ValueError as exc:
                        raise ValueError(
                            "checkpoint history cannot be decoded (JSON nesting too deep)"
                        ) from exc
        check_deadline()
    except sqlite3.OperationalError:
        if expired:
            raise ValueError("SQLite snapshot deadline exceeded") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)


def ingest_snapshot(
    source: Path,
    thread: str,
    *,
    error_channel: str,
    timeout: float = 30,
    graph: Any = None,
    include_error_messages: bool = True,
) -> RawTrace:
    from langgraph.checkpoint.sqlite import SqliteSaver

    deadline = time.monotonic() + timeout

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise ValueError("SQLite snapshot deadline exceeded")

    try:
        with tempfile.TemporaryDirectory(prefix="tracegraph-snapshot-") as directory:
            snapshot = Path(directory) / "checkpoints.sqlite"
            src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", timeout=timeout, uri=True)
            try:
                dest = sqlite3.connect(snapshot)
                try:
                    src.backup(dest, pages=256, progress=progress, sleep=0.01)
                finally:
                    dest.close()
            finally:
                src.close()
            # Not `from_conn_string`: it does not take a serializer, and the default one
            # will call whatever the payload names. `check_same_thread=False` mirrors what
            # that helper does.
            with closing(sqlite3.connect(str(snapshot), check_same_thread=False)) as conn:
                saver = SqliteSaver(conn, serde=_inert_serde())
                # Creates the tables in this private copy when absent, as the saver's first
                # listing would, so a database without them still reads as "no checkpoints".
                saver.setup()
                _check_json_depth(conn, thread, deadline)
                return LangGraphCheckpointAdapter(
                    saver,
                    error_channel=error_channel,
                    schedule_graph=graph,
                    include_error_messages=include_error_messages,
                ).ingest(thread)
    except sqlite3.Error as exc:
        raise ValueError("cannot read SQLite checkpoint snapshot") from exc
