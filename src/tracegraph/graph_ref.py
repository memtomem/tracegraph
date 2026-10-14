"""Load the compiled LangGraph graph named by ``ingest --graph`` and fingerprint it.

This is the one place tracegraph runs code it did not write. Importing the reference runs the
module's top-level code, and reading a checkpoint through the graph later runs the callbacks
the graph carries (state schema mappers and validators, reducers, custom channels, managed
values, cache-key functions), though LangGraph executes no node task. Nothing here is inferred:
the graph is used only when the caller names it.

A reference takes one of two forms, mirroring how Python itself would run the code:

* ``path/to/file.py:attr`` — the ``langgraph.json`` form. The file's directory is importable
  while it loads, as with ``python path/to/file.py``.
* ``pkg.module:attr`` — the current directory is importable while it loads, as with
  ``python -m pkg.module``.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _import_file(resolved: Path) -> Any:
    # A stable, collision-free module name. The module is registered before it executes, as
    # the import system does, because dataclasses and postponed annotations look their own
    # module up in sys.modules while the class body runs.
    name = "_tracegraph_graph_" + hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"{resolved} is not importable as a Python module")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Put back what a previous successful load registered: its graph may still be in use.
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module


def load_graph(ref: str) -> Any:
    """Import ``ref`` and return the compiled graph it names.

    Raises ``ValueError`` for every failure, including whatever the imported module raises
    (``SystemExit`` too), so a broken reference is a usage error rather than a crash.
    ``KeyboardInterrupt`` still propagates.
    """
    try:
        from langgraph.graph.state import StateGraph
        from langgraph.pregel import Pregel
    except ImportError as exc:
        raise ValueError("--graph requires langgraph to be installed") from exc

    target, sep, attr = ref.rpartition(":")
    if not sep or not target or not attr.isidentifier():
        raise ValueError(
            f"graph reference {ref!r} must be 'path/to/file.py:attr' or 'pkg.module:attr'"
        )
    is_file = target.endswith(".py") or "/" in target or os.sep in target
    if is_file and not Path(target).is_file():
        raise ValueError(f"cannot load graph {ref!r}: no such file {target!r}")
    search_dir = str(Path(target).resolve().parent) if is_file else os.getcwd()
    saved_path = list(sys.path)
    sys.path.insert(0, search_dir)
    missing = object()
    try:
        module = _import_file(Path(target).resolve()) if is_file else importlib.import_module(target)
        # Resolved once, inside the boundary: a module-level __getattr__ is user code too.
        graph = getattr(module, attr, missing)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise ValueError(f"cannot load graph {ref!r}: {_describe(exc)}") from exc
    finally:
        sys.path[:] = saved_path

    if graph is missing:
        raise ValueError(f"cannot load graph {ref!r}: the module has no attribute {attr!r}")
    if isinstance(graph, Pregel):
        return graph
    if isinstance(graph, StateGraph):
        raise ValueError(
            f"graph {ref!r} is an uncompiled StateGraph; name the result of .compile() instead"
        )
    raise ValueError(
        f"graph {ref!r} is a {type(graph).__name__}, not a compiled LangGraph graph"
    )


def fingerprint(graph: Any) -> str:
    """A ``sha256:`` digest of what decides which tasks the graph schedules from a checkpoint.

    Covers each node's trigger channels and input subscription, and each channel's name and
    class. It omits everything that does not change the schedule (node code, compile-time
    breakpoints, the checkpointer), so the same graph gives the same digest on any machine,
    under any hash seed, and across the LangGraph versions measured (1.2.2 and 1.2.15).
    """

    def _channels(value: Any) -> Any:
        return sorted(map(str, value)) if isinstance(value, (list, tuple)) else str(value)

    document = {
        "nodes": {
            name: {"triggers": sorted(map(str, node.triggers)), "input": _channels(node.channels)}
            for name, node in graph.nodes.items()
        },
        "channels": {name: type(channel).__name__ for name, channel in graph.channels.items()},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
