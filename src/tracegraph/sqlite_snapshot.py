"""Analyze checkpoints on a private SQLite backup, without initializing the source."""

from pathlib import Path
import sqlite3
import tempfile
import time

from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
from tracegraph.model import RawTrace


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
            with SqliteSaver.from_conn_string(str(snapshot)) as saver:
                return LangGraphCheckpointAdapter(saver, error_channel=error_channel).ingest(thread)
    except sqlite3.Error as exc:
        raise ValueError("cannot read SQLite checkpoint snapshot") from exc
