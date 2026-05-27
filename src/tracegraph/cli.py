"""``tracegraph`` command line — the demo-able surface: ingest / inspect / explain / diff.

This is a **checkpoint-level** view of a LangGraph thread: one node per super-step
checkpoint. Node names/kinds are best-effort display metadata (recovered heuristically from
the checkpoint stream), not an authoritative node-execution trace.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.tree import Tree

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import diff as tree_diff
from tracegraph.analysis import explain as explain_chain
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


def _load(path: Path) -> NormalizedTrace:
    """Load an artifact and validate both edge layers before using it."""
    nt = artifact.load(path)
    validate_normalized(nt)
    return nt


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


@app.command()
def inspect(artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`.")) -> None:
    """Render a trace's derived causal tree."""
    _render_tree(_load(artifact_path))


@app.command()
def explain(
    artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`."),
    step_id: str = typer.Argument(..., help="Step id to explain (full or unique suffix)."),
) -> None:
    """Trace a step's raw causal chain back toward its root cause."""
    store = InMemoryStore.load_artifact(artifact_path)
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
