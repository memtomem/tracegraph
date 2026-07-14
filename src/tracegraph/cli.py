"""``tracegraph`` command line for checkpoint, Phoenix, and OTLP causal analysis.

LangGraph checkpoint ingestion is a super-step view; Phoenix/OTLP ingestion is span-level.
Every source is normalized into the same raw causal DAG plus derived structural tree.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.tree import Tree

from tracegraph import artifact
from tracegraph.adapters import LangGraphCheckpointAdapter
from tracegraph.analysis import PRESETS
from tracegraph.analysis.diagnose import AnalysisReport
from tracegraph.analysis.diagnose import analyze as build_analysis
from tracegraph.analysis.diagnose import save_atomic as save_analysis_report
from tracegraph.analysis import Match
from tracegraph.analysis import diff as tree_diff
from tracegraph.analysis import explain as explain_chain
from tracegraph.analysis import search as pattern_search
from tracegraph.analysis import structure_only
from tracegraph.model import EdgeType, NormalizedTrace, StepStatus
from tracegraph.normalize import normalize, validate_normalized
from tracegraph.review_candidates import build_report as build_candidate_report
from tracegraph.review_candidates import is_review_exportable
from tracegraph.review_candidates import save_atomic as save_candidate_report
from tracegraph.store import InMemoryStore

app = typer.Typer(
    help="Causal-graph analysis of LangGraph, Phoenix, and OpenInference traces.",
    no_args_is_help=True,
    add_completion=False,
)
phoenix_app = typer.Typer(
    help="Read-only Phoenix CLI integration for body-free diagnosis.",
    no_args_is_help=True,
)
app.add_typer(phoenix_app, name="phoenix")
console = Console()
err_console = Console(stderr=True)  # diagnostics (warnings) — keep them off result stdout


class QueryBackend(str, Enum):
    MEMORY = "memory"
    LADYBUG = "ladybug"


# Exceptions that mean "this file isn't a usable tracegraph artifact" (bad path, non-JSON,
# wrong schema_version, or a structurally-invalid trace) — as opposed to a bug inside
# tracegraph. We translate these into clean CLI errors instead of dumping a raw traceback.
# ``json.JSONDecodeError``, ``UnicodeDecodeError`` and pydantic's ``ValidationError`` are all
# ``ValueError`` subclasses, as are the schema_version and canonical-form checks; ``OSError``
# covers missing / unreadable / is-a-directory paths. Anything else (e.g. a ``TypeError``)
# is a real bug and propagates unchanged.
#
# Trade-off: validate_normalized()'s canonical re-derivation also raises a bare ``ValueError``,
# so a non-canonical artifact is treated as bad input. That is correct for the common case (a
# corrupt or hand-edited file) and — in the rare case it surfaces a normalize() regression —
# the original message is preserved verbatim (see _load_reason) rather than masked.
_LOAD_ERRORS = (OSError, ValueError)


def _load_reason(exc: Exception) -> str:
    """A short, single-line reason for a load failure — no traceback, no stack."""
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    if isinstance(exc, IsADirectoryError):
        return "is a directory, not a file"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, UnicodeDecodeError):
        return "not UTF-8 text (is this a binary file?)"
    if isinstance(exc, json.JSONDecodeError):
        return "not valid JSON"
    if isinstance(exc, ValidationError):
        return "does not match the artifact schema"
    # schema_version mismatch and validate_normalized failures carry a useful message.
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__


def _load_validated(path: Path) -> NormalizedTrace:
    """Load an artifact and validate both edge layers. Propagates the underlying load error."""
    nt = artifact.load(path)
    validate_normalized(nt)
    return nt


def _read_source(source: str) -> str:
    if source == "-":
        return typer.get_text_stream("stdin").read()
    return Path(source).read_text(encoding="utf-8")


def _load_analysis_source(source: str, trace_id: str | None = None) -> NormalizedTrace:
    """Strictly identify a tracegraph artifact or Phoenix CLI trace export."""
    try:
        text = _read_source(source)
        payload = json.loads(text)
        if isinstance(payload, dict) and "schema_version" in payload:
            nt = artifact.loads(text)
        else:
            from tracegraph.adapters import PhoenixExportAdapter

            adapter = PhoenixExportAdapter(payload)
            ids = adapter.discover()
            selected = trace_id
            if selected is None:
                if len(ids) != 1:
                    raise ValueError(
                        f"Phoenix export holds {len(ids)} traces; pass --trace. "
                        f"Found: {', '.join(ids) or 'none'}"
                    )
                selected = ids[0]
            nt = normalize(adapter.ingest(selected))
        validate_normalized(nt)
        return nt
    except (*_LOAD_ERRORS, KeyError) as exc:
        raise typer.BadParameter(f"cannot load analysis input {source}: {_load_reason(exc)}") from exc


def _px_trace(trace_id: str) -> NormalizedTrace:
    """Read one trace through the official px CLI without persisting its raw body."""
    command = [
        "px",
        "trace",
        "get",
        trace_id,
        "--format",
        "raw",
        "--no-progress",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            "Phoenix CLI `px` was not found; install/configure @arizeai/phoenix-cli first"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise typer.BadParameter("px trace get timed out after 60 seconds") from exc
    except subprocess.CalledProcessError as exc:
        raise typer.BadParameter(
            f"px trace get failed with exit {exc.returncode}; verify Phoenix endpoint and authentication"
        ) from exc
    try:
        payload = json.loads(result.stdout)
        # `trace get` has no annotation flag. Fetch the same trace's annotated spans through
        # the documented read-only span command and merge only annotation objects by span id.
        try:
            annotated = subprocess.run(
                [
                    "px",
                    "span",
                    "list",
                    "--trace-id",
                    trace_id,
                    "--limit",
                    "10000",
                    "--include-annotations",
                    "--format",
                    "raw",
                    "--no-progress",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            span_payload = json.loads(annotated.stdout)
            annotated_spans = span_payload if isinstance(span_payload, list) else []
            annotations_by_id = {
                (item.get("context") or {}).get("span_id") or item.get("id"): item.get("annotations")
                for item in annotated_spans
                if isinstance(item, dict) and item.get("annotations")
            }
            for span in payload.get("spans", []) if isinstance(payload, dict) else []:
                span_id = (span.get("context") or {}).get("span_id") or span.get("id")
                if span_id in annotations_by_id:
                    span["annotations"] = annotations_by_id[span_id]
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
            ValueError,
        ):
            # Annotations are optional evidence. The trace itself remains diagnosable and
            # missing evaluations are rendered as unavailable, never as zero/pass.
            pass

        from tracegraph.adapters import PhoenixExportAdapter

        adapter = PhoenixExportAdapter(payload)
        return normalize(adapter.ingest(trace_id))
    except (ValueError, json.JSONDecodeError, KeyError) as exc:
        raise typer.BadParameter(
            f"px returned an unsupported or incomplete trace export: {_load_reason(exc)}"
        ) from exc


def _render_analysis(report: AnalysisReport, *, limit: int) -> None:
    status_style = "red" if report.status == StepStatus.ERROR.value else "green"
    console.print(
        f"[bold {status_style}]{escape(report.trace_id)}[/]  "
        f"{report.step_count} steps · {report.error_count} error"
    )

    console.print("\n[bold]What failed[/]")
    if not report.primary_failures:
        console.print("  [green]No error step found.[/]")
    for finding in report.primary_failures[:limit]:
        step = finding.step
        console.print(f"  [red]✗[/] {escape(step.name or step.kind)} ({step.kind}, step {step.seq})")
        shown = {item.step_id: item.name or item.kind for item in finding.causal_steps}
        shown[step.step_id] = step.name or step.kind
        for edge in finding.causal_edges:
            origin = f" [{edge.origin}]" if edge.origin else ""
            console.print(
                f"    {escape(shown.get(edge.cause, edge.cause))} → "
                f"{escape(shown.get(edge.effect, edge.effect))}[dim]{escape(origin)}[/]"
            )
    remaining = len(report.primary_failures) - limit
    if remaining > 0:
        console.print(f"  [dim]… {remaining} more primary failure(s) in JSON report[/]")
    if report.propagated_failures:
        names = ", ".join(escape(item.name or item.kind) for item in report.propagated_failures)
        console.print(f"  [dim]propagated/context errors: {names}[/]")

    console.print("\n[bold]Repeated behavior[/]")
    if report.patterns:
        for finding in report.patterns:
            labels = " → ".join(escape(label or "(unnamed)") for label in finding.labels)
            console.print(f"  • {finding.pattern_id}@v{finding.pattern_version}: {labels}")
    else:
        console.print("  [dim]No high-signal failure pattern found.[/]")

    metrics = report.metrics
    console.print("\n[bold]Telemetry[/]")
    console.print(
        "  wall="
        + (f"{metrics.wall_duration_ms:g} ms" if metrics.wall_duration_ms is not None else "unavailable")
        + " · tokens="
        + (str(metrics.total_tokens) if metrics.total_tokens is not None else "unavailable")
        + " · cost="
        + (
            f"{metrics.total_cost} {metrics.cost_currency or ''}".rstrip()
            if metrics.total_cost is not None
            else "unavailable"
        )
    )
    for evaluation in metrics.evaluations:
        console.print(
            f"  eval {escape(evaluation.name)}: "
            f"label={escape(evaluation.label or 'unavailable')} "
            f"score={evaluation.score if evaluation.score is not None else 'unavailable'}"
        )

    if report.decision_evidence:
        console.print("\n[bold]External decision evidence[/]")
        for item in report.decision_evidence:
            console.print(
                f"  {escape(item.source)} · verdict={escape(item.verdict)} · "
                f"generation={item.graph_generation} · "
                f"artifact={escape(item.artifact_digest)}"
            )

    if report.comparison:
        comparison = report.comparison
        console.print("\n[bold]Compared with baseline[/]")
        topology = "identical" if comparison.topology_identical else "changed"
        console.print(f"  topology: {topology}")
        for change in comparison.behavior_changes:
            console.print(
                f"  • {escape(change.name or change.logical_step_key)}: "
                f"{change.before or 'missing'} → {change.after or 'missing'}"
            )
        deltas = ", ".join(
            f"{key}={value:+}" for key, value in comparison.pattern_count_deltas.items() if value
        )
        if deltas:
            console.print(f"  pattern deltas: {deltas}")

    console.print("\n[bold]Data fidelity / privacy[/]")
    console.print(
        f"  {report.causal_fidelity} · links_preserved={report.links_preserved} · "
        f"privacy={report.privacy_profile}"
    )
    for warning in report.warnings:
        console.print(f"  [yellow]⚠ {escape(warning)}[/]")


def _artifact_files(paths: list[Path]) -> list[Path]:
    """Expand files and directories (``*.json``) into artifact paths."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    if not files:
        raise typer.BadParameter("no artifact files found")
    return files


@dataclass(frozen=True)
class _LoadedArtifact:
    path: Path
    trace: NormalizedTrace
    digest: str


def _load_artifact_evidence(path: Path) -> _LoadedArtifact:
    """Load and validate the exact bytes whose digest will identify review evidence."""
    raw = path.read_bytes()
    nt = artifact.loads(raw.decode("utf-8"))
    validate_normalized(nt)
    return _LoadedArtifact(
        path=path,
        trace=nt,
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
    )


def _load(path: Path) -> NormalizedTrace:
    """Load + validate an explicitly-named artifact, mapping failures to clean CLI errors."""
    try:
        return _load_validated(path)
    except _LOAD_ERRORS as exc:
        raise typer.BadParameter(f"cannot load artifact {path}: {_load_reason(exc)}") from exc


def _warn_skipped(skipped: list[tuple[Path, str]]) -> None:
    """Emit the stderr warning for directory-discovered files that weren't valid artifacts."""
    if not skipped:
        return
    # escape() the dynamic names/reasons so a filename containing Rich markup (e.g.
    # "weird[x].json") renders literally instead of being mis-parsed as style tags.
    detail = ", ".join(f"{f.name} ({why})" for f, why in skipped)
    err_console.print(
        f"[yellow]⚠ skipped {len(skipped)} non-artifact file(s): {escape(detail)}[/]"
    )


def _load_many_evidence(
    paths: list[Path], *, exclude: Path | None = None
) -> list[_LoadedArtifact]:
    """Expand files and directories (``*.json``) into a list of validated traces.

    Directory expansion is **lenient** and **non-recursive** (top-level ``*.json`` only,
    not subdirectories): a directory of artifacts may legitimately also hold unrelated JSON
    (configs, exports), so a globbed ``*.json`` that isn't a valid artifact is skipped (with
    a warning) rather than crashing the whole query. An **explicitly named** file is
    **strict**: if the user points directly at a file, a load failure is a clean, fatal
    error — they meant that file.

    Rejects duplicate ``trace_id`` across the loaded set. The portable artifact is a system
    of record keyed by ``trace_id``, so two files claiming the same id are ambiguous — and
    ``query --explain`` looks up the originating trace by id when rendering an ancestor
    chain, so silently keeping the last-loaded copy would attach matches from one artifact
    to a different artifact's causal graph.
    """
    discovered: list[tuple[Path, bool]] = []  # (path, explicitly_named)
    excluded = exclude.resolve() if exclude is not None else None
    for p in paths:
        if p.is_dir():
            discovered.extend(
                (f, False)
                for f in sorted(p.glob("*.json"))
                if excluded is None or f.resolve() != excluded
            )
        else:
            if excluded is not None and p.resolve() == excluded:
                raise typer.BadParameter("--out must not overwrite an input artifact")
            discovered.append((p, True))
    if not discovered:
        raise typer.BadParameter("no artifact files found")

    loaded: list[_LoadedArtifact] = []
    skipped: list[tuple[Path, str]] = []
    for f, explicit in discovered:
        if explicit:
            try:
                loaded.append(_load_artifact_evidence(f))
            except _LOAD_ERRORS as exc:
                # Flush what we've already skipped before aborting, so strays discovered
                # earlier in the argument list aren't silently dropped by the fatal error.
                _warn_skipped(skipped)
                raise typer.BadParameter(
                    f"cannot load artifact {f}: {_load_reason(exc)}"
                ) from exc
            continue
        try:
            loaded.append(_load_artifact_evidence(f))
        except _LOAD_ERRORS as exc:
            skipped.append((f, _load_reason(exc)))

    _warn_skipped(skipped)
    if not loaded:
        raise typer.BadParameter(
            f"no valid artifacts found ({len(skipped)} discovered file(s) were not "
            "tracegraph artifacts)"
        )

    seen: dict[str, Path] = {}
    for item in loaded:
        tid = item.trace.trace.trace_id
        if tid in seen:
            raise typer.BadParameter(
                f"duplicate trace_id {tid!r}: {seen[tid]} and {item.path}. Each artifact "
                "must carry a unique trace_id — rename or deduplicate before querying."
            )
        seen[tid] = item.path
    return loaded


def _load_many(paths: list[Path]) -> list[NormalizedTrace]:
    return [item.trace for item in _load_many_evidence(paths)]


def _store_cls(backend: QueryBackend) -> type[Any]:
    if backend is QueryBackend.MEMORY:
        return InMemoryStore
    try:
        from tracegraph.store.ladybug import LadybugStore
    except ModuleNotFoundError as exc:
        if exc.name != "ladybug":
            raise
        raise typer.BadParameter(
            "--backend ladybug requires the optional tracegraph[cypher] dependency"
        ) from exc
    return LadybugStore


def _store_from_artifact(path: Path, backend: QueryBackend) -> Any:
    # _store_cls may raise its own clean BadParameter (missing ladybug) — keep it outside the
    # try so we only translate *load* failures, not backend-selection ones.
    cls = _store_cls(backend)
    try:
        return cls.load_artifact(path)
    except _LOAD_ERRORS as exc:
        raise typer.BadParameter(f"cannot load artifact {path}: {_load_reason(exc)}") from exc


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
    fell_back = False
    for nt in traces:
        store = _store_from_trace(nt, backend)
        stores[nt.trace.trace_id] = store
        steps = nt.steps_by_id()
        for path in store.find_matches(pattern):
            labels = [steps[i].name or steps[i].kind.value for i in path]
            matches.append(Match(trace_id=nt.trace.trace_id, step_ids=path, labels=labels))
        fell_back = fell_back or getattr(store, "fell_back_to_python", False)
    if fell_back:
        # Honesty signal (off result stdout): the ladybug accelerator couldn't compile this
        # pattern under LadybugDB's 30-hop cap and ran the pure-Python matcher instead. Results
        # are identical — this just tells the user the accelerator deferred.
        err_console.print(
            "[dim]⚠ pattern not Cypher-compilable (unbounded gap); ran the pure-Python "
            "matcher over the raw causal graph — results are identical to --backend ladybug's.[/]"
        )
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

    def add_subtree(root_node: Tree, rid: str) -> None:
        # Iterative DFS: a recursive walk overflows on deep-linear traces (one super-step
        # per node). Push children in reverse-seq order so they render seq-ascending as they
        # pop, and each child's branch is rendered under its own parent.
        stack: list[tuple[Tree, str]] = [(root_node, rid)]
        while stack:
            rich_parent, nid = stack.pop()
            branch = rich_parent.add(_label(steps[nid]))
            for child in sorted(children.get(nid, []), key=lambda i: steps[i].seq, reverse=True):
                stack.append((branch, child))

    root_tree = Tree(f"[bold]{nt.trace.trace_id}[/] ({nt.trace.source_kind})")
    for rid in [i for i in order if i not in has_parent]:
        add_subtree(root_tree, rid)
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
    artifact.save_atomic(nt, target)
    console.print(f"[green]ingested {len(nt.steps)} steps[/] → {target}")


@app.command(name="ingest-otlp")
def ingest_otlp(
    file: Path = typer.Option(..., "--file", "-f", help="OTLP JSON/JSONL span export (resourceSpans)."),
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
    artifact.save_atomic(nt, target)
    console.print(f"[green]ingested {len(nt.steps)} spans[/] → {target}")


@app.command(name="ingest-phoenix")
def ingest_phoenix(
    file: str = typer.Option(..., "--file", "-f", help="Phoenix CLI trace JSON or '-' for stdin."),
    trace: str = typer.Option(None, "--trace", "-t", help="traceId (required for multi-trace exports)."),
    out: Path = typer.Option(None, "--out", "-o", help="Body-free artifact path."),
) -> None:
    """Ingest one Phoenix CLI trace export into a body-free portable artifact."""
    nt = _load_analysis_source(file, trace)
    if nt.trace.source_kind != "phoenix_cli":
        raise typer.BadParameter("--file must contain a Phoenix CLI trace export")
    target = out or Path(f"{nt.trace.trace_id}.json")
    artifact.save_atomic(nt, target)
    console.print(f"[green]ingested {len(nt.steps)} Phoenix spans[/] → {target}")


@app.command(name="analyze")
def analyze_command(
    source: str = typer.Argument(..., help="Tracegraph artifact, Phoenix export, or '-' for stdin."),
    trace: str = typer.Option(None, "--trace", "-t", help="Trace id for a multi-trace Phoenix input."),
    baseline: str = typer.Option(None, "--baseline", help="Explicit artifact/Phoenix baseline."),
    baseline_trace: str = typer.Option(None, "--baseline-trace", help="Trace id in a multi-trace baseline."),
    json_out: Path = typer.Option(None, "--json-out", help="Write deterministic body-free JSON report."),
    limit: int = typer.Option(3, "--limit", "-l", help="Primary failures rendered in the terminal."),
) -> None:
    """Automatically diagnose failures, retries, telemetry, and an optional baseline."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer")
    nt = _load_analysis_source(source, trace)
    baseline_nt = _load_analysis_source(baseline, baseline_trace) if baseline else None
    report = build_analysis(nt, baseline=baseline_nt)
    _render_analysis(report, limit=limit)
    if json_out:
        save_analysis_report(report, json_out)
        console.print(f"\n[green]report[/] → {json_out}")


@phoenix_app.command(name="diagnose")
def phoenix_diagnose(
    trace_id: str = typer.Argument(..., help="Phoenix trace id."),
    baseline: str = typer.Option(None, "--baseline", help="Explicit Phoenix baseline trace id."),
    save_artifact: Path = typer.Option(None, "--save-artifact", help="Save the body-free artifact."),
    json_out: Path = typer.Option(None, "--json-out", help="Write deterministic body-free JSON report."),
    limit: int = typer.Option(3, "--limit", "-l", help="Primary failures rendered in the terminal."),
) -> None:
    """Fetch through `px trace get` and diagnose without storing the raw export."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer")
    nt = _px_trace(trace_id)
    baseline_nt = _px_trace(baseline) if baseline else None
    report = build_analysis(nt, baseline=baseline_nt)
    _render_analysis(report, limit=limit)
    if save_artifact:
        artifact.save_atomic(nt, save_artifact)
        console.print(f"\n[green]artifact[/] → {save_artifact}")
    if json_out:
        save_analysis_report(report, json_out)
        console.print(f"[green]report[/] → {json_out}")


@app.command()
def inspect(artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`.")) -> None:
    """Render a trace's derived causal tree."""
    _render_tree(_load(artifact_path))


@app.command()
def validate(
    artifacts: list[Path] = typer.Argument(..., help="Artifact JSON files and/or directories."),
) -> None:
    """Validate artifacts against the canonical schema and causal-graph contract."""
    files = _artifact_files(artifacts)
    failures = 0
    for path in files:
        try:
            nt = _load(path)
        except Exception as exc:
            failures += 1
            console.print(f"[bold red]INVALID[/] {path}: {exc}")
            continue
        console.print(
            f"[green]OK[/] {path}  [dim]{nt.trace.trace_id} · {len(nt.steps)} steps[/]"
        )

    if failures:
        console.print(f"\n[bold red]{failures} invalid artifact(s)[/]")
        raise typer.Exit(1)
    console.print(f"\n[green]{len(files)} artifact(s) valid[/]")


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
    # An exact full-id match always wins — even when that id is also a *suffix* of a longer
    # namespaced id (e.g. "abc" vs "ns:abc"). Only fall back to suffix matching when the
    # input isn't itself a full step id, so a valid id is never rejected as "ambiguous".
    if step_id in steps:
        chosen = step_id
    else:
        suffix = [sid for sid in steps if sid.endswith(step_id)]
        if len(suffix) != 1:
            raise typer.BadParameter(
                f"{step_id!r} matched {len(suffix)} steps; use a unique id or suffix."
            )
        chosen = suffix[0]
    result = explain_chain(store, chosen)
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
        # escape() the pattern: its rendering uses [gap …] markers that Rich would otherwise
        # parse as style tags and silently swallow (the gap connector would vanish).
        version = f"@v{pattern.pattern_version}"
        review = "  [cyan]review-exportable[/]" if is_review_exportable(pattern) else ""
        console.print(
            f"[bold]{name}{version}[/]  [dim]{escape(pattern.description)}[/]{review}"
        )
        console.print(f"    {escape(str(pattern))}")


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
    # escape() the pattern str (its [gap …] markers would be eaten as Rich markup otherwise).
    console.print(
        f"[bold]{pattern.pattern_id}@v{pattern.pattern_version}[/]: {escape(str(pattern))}  "
        f"[dim](over {len(traces)} trace(s))[/]\n"
    )
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


@app.command(name="export-review-candidates")
def export_review_candidates(
    preset: str = typer.Argument(..., help="Review-exportable preset pattern name."),
    artifacts: list[Path] = typer.Argument(..., help="Artifact JSON files and/or directories."),
    out: Path = typer.Option(..., "--out", "-o", help="Versioned review-candidate JSON report."),
    backend: QueryBackend = typer.Option(
        QueryBackend.MEMORY,
        "--backend",
        case_sensitive=False,
        help="Pattern-matching backend; both backends produce identical candidates.",
    ),
) -> None:
    """Export body-free, versioned governance review candidates. Empty matches succeed."""
    pattern = PRESETS.get(preset)
    if pattern is None:
        raise typer.BadParameter(
            f"unknown preset {preset!r}; available: {', '.join(PRESETS)}"
        )
    if not is_review_exportable(pattern):
        raise typer.BadParameter(f"preset {preset!r} is not review-exportable")

    loaded = _load_many_evidence(artifacts, exclude=out)
    traces = [item.trace for item in loaded]
    matches, _ = _search_with_backend(traces, pattern, backend)
    traces_by_id = {item.trace.trace.trace_id: item.trace for item in loaded}
    digests_by_id = {item.trace.trace.trace_id: item.digest for item in loaded}
    try:
        report = build_candidate_report(pattern, matches, traces_by_id, digests_by_id)
        save_candidate_report(report, out)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"cannot export review candidates: {exc}") from exc
    console.print(
        f"[green]exported {len(report.candidates)} review candidate(s)[/] "
        f"from {len(matches)} match(es) → {out}"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
