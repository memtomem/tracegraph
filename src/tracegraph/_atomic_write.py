"""Write a text file atomically: a reader sees either the previous file or the complete new one."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def write_text_atomic(
    path: str | Path, text: str, *, replace: bool = True, newline: str | None = None
) -> None:
    """Write ``text`` to a temporary file beside ``path``, fsync it, then publish it.

    ``replace=False`` publishes with a hard link, which fails with ``FileExistsError``
    instead of overwriting an existing file or following a symlink at ``path``. The
    temporary file is removed on every exit path, including a failed write or fsync.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline=newline,
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temp_name, target)
        else:
            os.link(temp_name, target)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
