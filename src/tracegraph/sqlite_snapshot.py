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

from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
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


def ingest_snapshot(source: Path, thread: str, *, error_channel: str, timeout: float = 30) -> RawTrace:
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
                return LangGraphCheckpointAdapter(saver, error_channel=error_channel).ingest(thread)
    except sqlite3.Error as exc:
        raise ValueError("cannot read SQLite checkpoint snapshot") from exc
