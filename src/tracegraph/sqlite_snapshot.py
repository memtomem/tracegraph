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
import errno
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Callable

from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
from tracegraph.input_validation import check_msgpack_depth, check_text_depth
from tracegraph.model import RawTrace

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


_MSGPACK_REFUSED = "checkpoint history cannot be decoded (msgpack nesting too deep)"
_JSON_REFUSED = "checkpoint history cannot be decoded (JSON nesting too deep)"
_DEADLINE_EXCEEDED = "SQLite snapshot deadline exceeded"


def _deadline_tick(deadline: float) -> Callable[[], None]:
    def tick() -> None:
        if time.monotonic() >= deadline:
            raise ValueError(_DEADLINE_EXCEEDED)

    return tick


def _scanning_serde(deadline: float) -> tuple[Any, list[str]]:
    """``_inert_serde`` that measures every msgpack and JSON value before decoding it, and its refusal.

    LangGraph's decoder unpacks each extension payload with a new call, so stacked extensions
    nest past ormsgpack's per-call limit until the C stack overflows, with no Python exception
    (``check_msgpack_depth``). Python 3.12 and 3.13's ``json`` refuse deep nesting and 3.14's
    parses it, so a JSON text is measured first (``check_text_depth``) to refuse it the same
    way on every interpreter. Both checks run in ``loads_typed`` so that they cover exactly the
    values the saver decodes: SQL written to select the same rows can be steered elsewhere by a
    crafted schema's collations. The adapter reports any decode failure as ``cannot be decoded
    (<type>)``, so the reason is also kept in the returned list, which ``ingest_snapshot``
    raises instead. The list is shared by reference with any copy the saver makes
    (``with_msgpack_allowlist`` copies the serializer), and once set every later decode is
    refused too, so a caller that swallows one failure cannot decode on past it. Each check is
    charged to ``deadline`` before it starts and once it ends, and the msgpack walk also every
    65,536 objects; depth found during a check is reported as depth even if the deadline has
    also passed.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    refusal: list[str] = []
    tick = _deadline_tick(deadline)

    class _Scanning(JsonPlusSerializer):
        def loads_typed(self, data: tuple[str, bytes]) -> Any:
            if refusal:
                raise ValueError(refusal[0])
            if data[0] == "msgpack":
                try:
                    tick()
                    check_msgpack_depth(data[1], tick)
                    tick()
                except ValueError as exc:
                    nested = str(exc) == "msgpack nesting too deep"
                    refusal.append(_MSGPACK_REFUSED if nested else str(exc))
                    raise ValueError(refusal[0]) from None
            elif data[0] == "json" and isinstance(data[1], (str, bytes)):
                try:
                    tick()
                    check_text_depth(data[1])
                    tick()
                except ValueError as exc:
                    nested = str(exc) == "JSON nesting too deep"
                    refusal.append(_JSON_REFUSED if nested else str(exc))
                    raise ValueError(refusal[0]) from None
            return super().loads_typed(data)

    return _Scanning(allowed_msgpack_modules=()), refusal


def _scanning_rows(deadline: float, refusal: list[str]) -> Callable[[sqlite3.Cursor, tuple], tuple]:
    """A row factory that measures each checkpoint's metadata in the row the saver fetches.

    ``SqliteSaver`` parses metadata with plain ``json.loads``, outside the serializer
    ``_scanning_serde`` checks, so the text is measured here, in the row object the saver then
    unpacks and parses: SQL written to read "the same" rows first can be handed different
    values by a ``checkpoints`` view or virtual table. The result column carries the spelling
    the schema declared (``METADATA``, a view's ``AS MetaData``), not the query's, so it is
    matched as SQLite matches identifiers, ignoring case. Shares ``refusal`` with
    ``_scanning_serde``: once either refuses, every later row is refused too. Each check is
    charged to ``deadline`` before it starts and once it ends.
    """
    tick = _deadline_tick(deadline)

    def factory(cursor: sqlite3.Cursor, row: tuple) -> tuple:
        if refusal:
            raise ValueError(refusal[0])
        for column, value in zip(cursor.description, row, strict=True):
            if isinstance(value, (str, bytes)) and column[0].lower() == "metadata":
                try:
                    tick()
                    check_text_depth(value)
                    tick()
                except ValueError as exc:
                    nested = str(exc) == "JSON nesting too deep"
                    refusal.append(_JSON_REFUSED if nested else str(exc))
                    raise ValueError(refusal[0]) from None
        return row

    return factory


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

    try:
        unusable = (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or math.isnan(timeout)
        )
    except OverflowError:  # an int too large for a float
        unusable = True
    if unusable:
        raise ValueError("SQLite snapshot deadline exceeded")
    source = Path(source)
    try:
        resolved_uri = source.resolve().as_uri()
    except RuntimeError as exc:
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(source)) from exc
    deadline = time.monotonic() + timeout

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() >= deadline:
            raise ValueError("SQLite snapshot deadline exceeded")

    try:
        with tempfile.TemporaryDirectory(prefix="tracegraph-snapshot-") as directory:
            snapshot = Path(directory) / "checkpoints.sqlite"
            src = sqlite3.connect(resolved_uri + "?mode=ro", timeout=timeout, uri=True)
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
                serde, refusal = _scanning_serde(deadline)
                conn.row_factory = _scanning_rows(deadline, refusal)
                saver = SqliteSaver(conn, serde=serde)
                # Creates the tables in this private copy when absent, as the saver's first
                # listing would, so a database without them still reads as "no checkpoints".
                saver.setup()
                expired = False

                def interrupt() -> int:
                    nonlocal expired
                    expired = time.monotonic() >= deadline
                    return expired

                conn.set_progress_handler(interrupt, 10_000)
                try:
                    raw = LangGraphCheckpointAdapter(
                        saver,
                        error_channel=error_channel,
                        schedule_graph=graph,
                        include_error_messages=include_error_messages,
                    ).ingest(thread)
                    if refusal:  # a decode failure the adapter did not let through
                        raise ValueError(refusal[0])
                    if time.monotonic() >= deadline:
                        raise ValueError("SQLite snapshot deadline exceeded")
                    return raw
                except KeyError:
                    # No checkpoints: no row reached the factory's deadline check, and a short
                    # query can run long without reaching the progress handler.
                    if time.monotonic() >= deadline:
                        raise ValueError(_DEADLINE_EXCEEDED) from None
                    raise
                except ValueError as exc:
                    if refusal:
                        raise ValueError(refusal[0]) from None
                    if expired and isinstance(exc.__cause__, sqlite3.OperationalError):
                        raise ValueError("SQLite snapshot deadline exceeded") from None
                    raise
                finally:
                    conn.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        raise ValueError("cannot read SQLite checkpoint snapshot") from exc
