"""tracegraph — causal-graph analysis of LangGraph agent traces."""

from importlib.metadata import PackageNotFoundError, version as _version

from tracegraph.model import (
    Edge,
    EdgeType,
    NormalizedTrace,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)

try:
    #: Read from the installed distribution so there is exactly one source of truth for the
    #: version — pyproject.toml. A hard-coded literal here would silently drift from it.
    __version__ = _version("tracegraph")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0.dev0"

__all__ = [
    "__version__",
    "Edge",
    "EdgeType",
    "NormalizedTrace",
    "RawTrace",
    "Step",
    "StepKind",
    "StepSource",
    "StepStatus",
    "Trace",
]
