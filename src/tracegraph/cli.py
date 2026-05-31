"""``tracegraph`` command line — the demo-able surface: ingest / inspect / explain / diff.

This is a **checkpoint-level** view of a LangGraph thread: one node per super-step
checkpoint. Node names/kinds are best-effort display metadata (recovered heuristically from
the checkpoint stream), not an authoritative node-execution trace.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.tree import Tree

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import PRESETS
from tracegraph.analysis import Match
from tracegraph.analysis import diff as tree_diff
from tracegraph.analysis import explain as explain_chain
from tracegraph.analysis import search as pattern_search
from tracegraph.analysis import structure_only
from tracegraph.model import EdgeType, NormalizedTrace, StepStatus
from tracegraph.normalize import normalize, validate_normalized
from tracegraph.store import InMemoryStore

app = typer.Typer(
    help="Causal-graph analysis of LangGraph agent traces (checkpoint-level view).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


class QueryBackend(str, Enum):
    MEMORY = "memory"
    KUZU = "kuzu"


def _load(path: Path) -> NormalizedTrace:
    """Load an artifact and validate both edge layers before using it."""
    nt = artifact.load(path)
    validate_normalized(nt)
    return nt


def _load_many(paths: list[Path]) -> list[NormalizedTrace]:
    """Expand files and directories (``*.json``) into a list of validated traces.

    Rejects duplicate ``trace_id`` across the input set. The portable artifact is a
    system of record keyed by ``trace_id``, so two files claiming the same id are
    ambiguous — and ``query --explain`` looks up the originating trace by id when
    rendering an ancestor chain, so silently keeping the last-loaded copy would
    attach matches from one artifact to a different artifact's causal graph.
    """
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    if not files:
        raise typer.BadParameter("no artifact files found")
    traces = [_load(f) for f in files]
    seen: dict[str, Path] = {}
    for tr, f in zip(traces, files):
        tid = tr.trace.trace_id
        if tid in seen:
            raise typer.BadParameter(
                f"duplicate trace_id {tid!r}: {seen[tid]} and {f}. Each artifact "
                "must carry a unique trace_id — rename or deduplicate before querying."
            )
        seen[tid] = f
    return traces


def _store_cls(backend: QueryBackend) -> type[Any]:
    if backend is QueryBackend.MEMORY:
        return InMemoryStore
    try:
        from tracegraph.store.kuzu import KuzuStore
    except ModuleNotFoundError as exc:
        if exc.name != "kuzu":
            raise
        raise typer.BadParameter(
            "--backend kuzu requires the optional tracegraph[cypher] dependency"
        ) from exc
    return KuzuStore


def _store_from_artifact(path: Path, backend: QueryBackend) -> Any:
    return _store_cls(backend).load_artifact(path)


def _store_from_trace(nt: NormalizedTrace, backend: QueryBackend) -> Any:
    return _store_cls(backend).from_trace(nt)


def _search_with_backend(
    traces: list[NormalizedTrace],
    pattern,
    backend: QueryBackend,
) -> tuple[list[Match], dict[str, Any]]:
    if backend is QueryBackend.MEMORY:
        return pattern_search(traces, pattern), {}

    matches: list[Match] = []
    stores: dict[str, Any] = {}
    for nt in traces:
        store = _store_from_trace(nt, backend)
        stores[nt.trace.trace_id] = store
        steps = nt.steps_by_id()
        for path in store.find_matches(pattern):
            labels = [steps[i].name or steps[i].kind.value for i in path]
            matches.append(Match(trace_id=nt.trace.trace_id, step_ids=path, labels=labels))
    return matches, stores


def _label(step) -> str:
    base = step.name or step.kind.value
    suffix = "  ⚠ lossy-projection" if step.projection_lossy else ""
    if step.status is StepStatus.ERROR:
        return f"[bold red]{step.seq}: {base}  ✗ {step.error_msg or 'error'}[/]{suffix}"
    return f"[green]{step.seq}: {base}[/]{suffix}"


def _render_tree(nt: NormalizedTrace) -> None:
    children: dict[str, list[str]] = {}
    for e in nt.edges_of(EdgeType.TREE_PARENT):
        children.setdefault(e.dst, []).append(e.src)
    has_parent = {e.src for e in nt.edges_of(EdgeType.TREE_PARENT)}
    steps = nt.steps_by_id()
    order = sorted(children.keys() | {s.step_id for s in nt.steps}, key=lambda i: steps[i].seq)

    def add(parent_node: Tree, nid: str) -> None:
        branch = parent_node.add(_label(steps[nid]))
        for child in sorted(children.get(nid, []), key=lambda i: steps[i].seq):
            add(branch, child)

    root_tree = Tree(f"[bold]{nt.trace.trace_id}[/] ({nt.trace.source_kind})")
    for rid in [i for i in order if i not in has_parent]:
        add(root_tree, rid)
    console.print(root_tree)

    n_err = sum(1 for s in nt.steps if s.status is StepStatus.ERROR)
    n_tool = sum(1 for s in nt.steps if s.kind.value == "TOOL")
    n_lossy = sum(1 for s in nt.steps if s.projection_lossy)
    console.print(
        f"\n[dim]{len(nt.steps)} steps · {n_tool} tool · "
        f"{n_err} error · {n_lossy} lossy-projection[/]"
    )


@app.command()
def ingest(
    sqlite: Path = typer.Option(..., help="Path to a LangGraph SqliteSaver DB file."),
    thread: str = typer.Option(..., "--thread", "-t", help="thread_id to ingest."),
    out: Path = typer.Option(None, "--out", "-o", help="Artifact path (default <thread>.json)."),
    error_channel: str = typer.Option("error", help="State channel that signals a step error."),
) -> None:
    """Ingest a LangGraph thread's checkpoint history into a portable artifact."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    target = out or Path(f"{thread}.json")
    with SqliteSaver.from_conn_string(str(sqlite)) as saver:
        raw = LangGraphCheckpointAdapter(saver, error_channel=error_channel).ingest(thread)
    nt = normalize(raw)
    artifact.save(nt, target)
    console.print(f"[green]ingested {len(nt.steps)} steps[/] → {target}")


@app.command(name="ingest-otlp")
def ingest_otlp(
    file: Path = typer.Option(..., "--file", "-f", help="OTLP/JSON span export (resourceSpans)."),
    trace: str = typer.Option(None, "--trace", "-t", help="traceId to ingest (default: the file's only trace)."),
    out: Path = typer.Option(None, "--out", "-o", help="Artifact path (default <traceId>.json)."),
) -> None:
    """Ingest one trace from an OpenInference/OTLP span export into a portable artifact."""
    from tracegraph.adapters import OTLPSpanAdapter

    adapter = OTLPSpanAdapter.from_file(file)
    if trace is None:
        ids = adapter.discover()
        if len(ids) != 1:
            raise typer.BadParameter(
                f"file holds {len(ids)} traces; pass --trace. Found: {', '.join(ids) or 'none'}"
            )
        trace = ids[0]
    nt = normalize(adapter.ingest(trace))
    target = out or Path(f"{trace}.json")
    artifact.save(nt, target)
    console.print(f"[green]ingested {len(nt.steps)} spans[/] → {target}")


@app.command()
def inspect(artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`.")) -> None:
    """Render a trace's derived causal tree."""
    _render_tree(_load(artifact_path))


@app.command()
def explain(
    artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`."),
    step_id: str = typer.Argument(..., help="Step id to explain (full or unique suffix)."),
    backend: QueryBackend = typer.Option(
        QueryBackend.MEMORY,
        "--backend",
        case_sensitive=False,
        help="Query backend to use for raw causal traversal.",
    ),
) -> None:
    """Trace a step's raw causal chain back toward its root cause."""
    store = _store_from_artifact(artifact_path, backend)
    steps = store.trace().steps_by_id()
    matches = [sid for sid in steps if sid == step_id or sid.endswith(step_id)]
    if len(matches) != 1:
        raise typer.BadParameter(
            f"{step_id!r} matched {len(matches)} steps; use a unique id/suffix."
        )
    result = explain_chain(store, matches[0])
    target = result.target
    console.print(f"[bold]{target.name or target.kind.value}[/] (step {target.seq}) "
                  f"[{target.status.value}] {target.error_msg or ''}")
    if not result.chain:
        console.print("[dim]no causes (this is a root step)[/]")
    for s in result.chain:
        flag = " [yellow]⚠ projection-lossy[/]" if s.projection_lossy else ""
        console.print(f"  ← {s.name or s.source.value} (step {s.seq}){flag}")
    if result.is_lossy:
        console.print(
            "\n[yellow]Some steps had multiple real causes; the single-parent tree view "
            "is lossy for them — trust this raw chain, not the tree.[/]"
        )


@app.command()
def diff(
    a: Path = typer.Argument(..., help="Artifact JSON for run A."),
    b: Path = typer.Argument(..., help="Artifact JSON for run B."),
    structure: bool = typer.Option(False, "--structure", help="Compare topology only, ignore labels."),
) -> None:
    """Structurally diff two runs (rooted-tree isomorphism over the derived tree).

    Exits 0 when isomorphic, 1 when not (so CI can gate on structural regressions).
    """
    nt_a, nt_b = _load(a), _load(b)
    label = structure_only if structure else None
    result = tree_diff(nt_a, nt_b, label) if label else tree_diff(nt_a, nt_b)
    if result.identical:
        console.print("[green]IDENTICAL[/] — the two runs are structurally isomorphic.")
        raise typer.Exit(0)
    console.print("[bold yellow]NOT IDENTICAL[/]")
    for line in result.changes:
        console.print(f"  • {line}")
    console.print("[dim](divergence localization is heuristic; the identical/not verdict is exact)[/]")
    raise typer.Exit(1)


@app.command()
def presets() -> None:
    """List the named cross-trace query patterns."""
    for name, pattern in PRESETS.items():
        console.print(f"[bold]{name}[/]  [dim]{pattern.description}[/]")
        console.print(f"    {pattern}")


@app.command()
def query(
    preset: str = typer.Argument(..., help="Preset pattern name (see `tracegraph presets`)."),
    artifacts: list[Path] = typer.Argument(..., help="Artifact JSON files and/or directories."),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-l",
        help="Cap total matches across all traces (useful when scanning hundreds). "
             "Match order is unchanged — the cap simply truncates the tail.",
    ),
    explain: bool = typer.Option(
        False,
        "--explain",
        help="For each match, render the raw causal ancestor chain of the matched "
             "effect (the last step in the path) — same shape as `tracegraph explain`.",
    ),
    backend: QueryBackend = typer.Option(
        QueryBackend.MEMORY,
        "--backend",
        case_sensitive=False,
        help="Query backend to use for pattern matching and optional explanations.",
    ),
) -> None:
    """Find a causal pattern across one or many traces. Exits 1 if no match is found."""
    pattern = PRESETS.get(preset)
    if pattern is None:
        raise typer.BadParameter(
            f"unknown preset {preset!r}; available: {', '.join(PRESETS)}"
        )
    if limit is not None and limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer")
    traces = _load_many(artifacts)
    matches, stores = _search_with_backend(traces, pattern, backend)
    total = len(matches)
    truncated = limit is not None and total > limit
    if truncated:
        matches = matches[:limit]
    console.print(f"[bold]{preset}[/]: {pattern}  [dim](over {len(traces)} trace(s))[/]\n")
    if not matches:
        console.print("[dim]no matches[/]")
        raise typer.Exit(1)

    # Lazily build stores only for the traces that actually appear in the (possibly
    # truncated) match set — --explain over a 10-of-500 cap shouldn't pay 500 store loads.
    traces_by_id = {nt.trace.trace_id: nt for nt in traces}
    for m in matches:
        console.print(f"[green]{m.trace_id}[/]: " + " → ".join(m.labels))
        if explain:
            store = stores.get(m.trace_id)
            if store is None:
                store = _store_from_trace(traces_by_id[m.trace_id], backend)
                stores[m.trace_id] = store
            # The effect (last step in the matched path) is what we trace back from —
            # the rest of the match is by construction part of its causal chain, but
            # explain() surfaces every cause including ones the pattern didn't constrain.
            result = explain_chain(store, m.step_ids[-1])
            if not result.chain:
                console.print("    [dim]← (no causes — matched effect is a root step)[/]")
            for s in result.chain:
                flag = " [yellow]⚠ projection-lossy[/]" if s.projection_lossy else ""
                # Fallback label matches `tracegraph explain` (s.name or s.source.value)
                # so the two renderings agree for nameless LangGraph checkpoints.
                console.print(f"    ← {s.name or s.source.value} (step {s.seq}){flag}")
            # Causal-honesty signal: when the matched effect (or anything in the chain)
            # has more than one real cause, the single-parent tree projection dropped
            # at least one. `tracegraph explain` surfaces this same warning — `query
            # --explain` must too, or a fan-in effect's lossiness becomes invisible
            # (target lossiness doesn't show up in the per-step ⚠ flags above, since
            # we only flag chain ancestors there).
            if result.is_lossy:
                console.print(
                    "    [yellow]⚠ some steps in this match had multiple real causes — "
                    "trust this raw chain, not the tree.[/]"
                )

    n_traces = len({m.trace_id for m in matches})
    suffix = f" — truncated from {total}" if truncated else ""
    console.print(
        f"\n[dim]{len(matches)} match(es) across {n_traces} trace(s){suffix}[/]"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
