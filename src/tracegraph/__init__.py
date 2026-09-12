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
    #: The *distribution* name, which is not the import name: `tracegraph` was taken on PyPI.
    #: Getting this wrong is silent — the lookup raises PackageNotFoundError and the except
    #: branch below reports 0.0.0.dev0 for a perfectly good install.
    __version__ = _version("agent-tracegraph")
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
