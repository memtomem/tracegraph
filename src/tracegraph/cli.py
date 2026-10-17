"""``tracegraph`` command line for checkpoint, Phoenix, and OTLP causal analysis.

LangGraph checkpoint ingestion is a super-step view; Phoenix/OTLP ingestion is span-level.
Every source is normalized into the same raw causal DAG plus derived structural tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.tree import Tree

from tracegraph import artifact
from tracegraph.input_validation import loads as load_json
from tracegraph.analysis import PRESETS
from tracegraph.analysis.diagnose import AnalysisReport
from tracegraph.analysis.diagnose import analyze as build_analysis
from tracegraph.analysis.diagnose import save_atomic as save_analysis_report
from tracegraph.analysis import Match
from tracegraph.analysis.patterns import match_for
from tracegraph.analysis import diff as tree_diff
from tracegraph.analysis import explain as explain_chain
from tracegraph.analysis import search as pattern_search
from tracegraph import __version__
from tracegraph.analysis import default_label, structure_only
from tracegraph.model import EdgeType, NormalizedTrace, RawTrace, StepSource, StepStatus
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


def _version_callback(value: bool) -> None:
    if value:
        # Printed bare: this is the one output a release script parses, and a prefix would
        # make every consumer strip it.
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        help="Print the installed version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Causal-graph analysis of LangGraph, Phoenix, and OpenInference traces."""


console = Console()
err_console = Console(stderr=True)  # diagnostics (warnings) — keep them off result stdout

#: Per-file ceiling for artifact loads. An artifact is a body-free trace (hundreds of
#: steps, a few MB at most); anything this large is not one, and parsing it would pin
#: multiples of its size in memory. Override with TRACEGRAPH_MAX_ARTIFACT_BYTES
#: (0 disables the check) if real artifacts ever approach it.
_DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def _max_artifact_bytes() -> int:
    raw = os.environ.get("TRACEGRAPH_MAX_ARTIFACT_BYTES")
    if raw is None:
        return _DEFAULT_MAX_ARTIFACT_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise typer.BadParameter(
            f"TRACEGRAPH_MAX_ARTIFACT_BYTES must be an integer, got {raw!r}"
        ) from None
    if value < 0:
        raise typer.BadParameter("TRACEGRAPH_MAX_ARTIFACT_BYTES must be >= 0")
    if value > 2**63 - 2:
        # Sanity bound: nothing on disk approaches this, and byte counts beyond it stop
        # being meaningful sizes. The chunked reader never passes the limit to read().
        raise typer.BadParameter(
            f"TRACEGRAPH_MAX_ARTIFACT_BYTES is too large (maximum {2**63 - 2})"
        )
    return value

_PX_MIN_VERSION = (1, 0, 4)
_PX_SCAN_LIMIT = 20
_PX_TIMEOUT_SECONDS = 60
_PX_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")


class QueryBackend(str, Enum):
    MEMORY = "memory"
    LADYBUG = "ladybug"


class _PxError(RuntimeError):
    """An actionable, body-free failure at the external Phoenix CLI boundary."""


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


def _load_error(path: Path | str, exc: Exception) -> typer.BadParameter:
    return typer.BadParameter(f"cannot load artifact {path}: {_load_reason(exc)}")


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


#: Chunk size for bounded artifact reads. Fixed-size chunks with a cumulative cap —
#: never ``read(limit + 1)``, which pre-allocates the whole buffer and fails outright
#: (MemoryError/OverflowError) for a large configured limit even on a tiny file.
_READ_CHUNK_BYTES = 8 * 1024 * 1024


def _read_artifact_bytes(path: Path) -> bytearray:
    """Read a whole artifact file, enforcing the byte ceiling on the actual reads.

    Bounded chunked reads from one open descriptor — not a ``stat()`` pre-check — so a
    file that grows (or lies about its size, e.g. a FIFO) between check and read can
    never pull more than ``limit + 1`` bytes into memory: each read requests at most
    the remaining allowance, and one byte past the limit is enough to reject. The
    result accumulates into a single ``bytearray`` (no final full-size join copy).
    This is the single file-load boundary every CLI path goes through.
    """
    with open(path, "rb") as handle:
        return _read_bounded(handle)


def _read_bounded(handle: Any) -> bytearray:
    """Bounded chunked reads from an open binary handle (a file or stdin)."""
    limit = _max_artifact_bytes()
    buf = bytearray()
    while True:
        want = (
            min(_READ_CHUNK_BYTES, limit + 1 - len(buf))
            if limit
            else _READ_CHUNK_BYTES
        )
        chunk = handle.read(want)
        if not chunk:
            break
        buf += chunk
        if limit and len(buf) > limit:
            raise ValueError(
                f"artifact exceeds the {limit}-byte limit "
                "(set TRACEGRAPH_MAX_ARTIFACT_BYTES to raise it, 0 to disable)"
            )
    return buf


def _load_validated(path: Path) -> NormalizedTrace:
    """Load an artifact and validate both edge layers. Propagates the underlying load error."""
    nt = artifact.loads(_read_artifact_bytes(path).decode("utf-8"))
    validate_normalized(nt)
    return nt


def _read_source(source: str) -> str:
    """Read a file or stdin (``-``) through the same byte ceiling."""
    if source == "-":
        return _read_bounded(typer.get_binary_stream("stdin")).decode("utf-8")
    return _read_artifact_bytes(Path(source)).decode("utf-8")


@dataclass(frozen=True)
class _AnalysisInput:
    trace: NormalizedTrace
    #: "artifact", "otlp" or "phoenix": which reader produced ``trace``.
    source: str


_OTLP_KEYS = ("resourceSpans", "resource_spans")
_PHOENIX_ROOT_KEYS = ("traces", "data")
_SHAPES_HELP = (
    "expected a tracegraph artifact (schema_version), an OTLP span export (resourceSpans), "
    "or a Phoenix CLI trace export (spans/traces/data)"
)


def _families(record: Any, *, root: bool) -> set[str]:
    """Which readers' top-level keys one JSON value carries: "otlp", "phoenix", "artifact".

    Only routing is decided here; whether a record is *valid* for its reader is the adapter's
    call, so the direct path accepts and rejects exactly what the explicit ingest commands do.
    Phoenix's ``traces``/``data`` envelopes count only at a document root, where the adapter
    accepts them.
    """
    if not isinstance(record, dict):
        return set()
    found = set()
    if "schema_version" in record:
        found.add("artifact")
    if any(key in record for key in _OTLP_KEYS):
        found.add("otlp")
    phoenix_keys = ("spans", *_PHOENIX_ROOT_KEYS) if root else ("spans",)
    if any(isinstance(record.get(key), list) for key in phoenix_keys):
        found.add("phoenix")
        for key in _PHOENIX_ROOT_KEYS if root else ():
            if isinstance(record.get(key), list):
                nested = set().union(*(_families(item, root=False) for item in record[key]))
                found |= {"nested-artifact" if k == "artifact" else k for k in nested} - {"phoenix"}
    return found


def _route(records: list[Any], *, root: bool, container: str | None) -> str:
    """The one reader for these records, or a ValueError whose advice is always executable."""
    found = set().union(*(_families(item, root=root) for item in records)) if records else set()
    if "nested-artifact" in found or ("artifact" in found and container is not None):
        raise ValueError(
            "unsupported shape; a tracegraph artifact (schema_version) must be the whole "
            "document, not a record inside a list, JSON Lines, or a Phoenix envelope"
        )
    if {"otlp", "phoenix"} <= found:
        how = (
            "extract the records of one format into their own file and read it with "
            "ingest-otlp (OTLP) or ingest-phoenix (Phoenix)"
            if container is not None or any(
                isinstance(records[0].get(k), list) for k in _PHOENIX_ROOT_KEYS)
            else "ingest-otlp reads it as OTLP and ingest-phoenix reads it as Phoenix"
        )
        raise ValueError(
            "ambiguous input: carries both OTLP (resourceSpans) and Phoenix (spans/traces/data) "
            f"records; {how}"
        )
    for kind in ("otlp", "phoenix"):
        if kind in found:
            return kind
    raise ValueError(f"unsupported shape; {_SHAPES_HELP}")


def _parse_analysis_text(text: str) -> tuple[str, Any]:
    """Identify an artifact, OTLP export (JSON or Collector JSONL) or Phoenix export by shape.

    Routing is decided before any adapter runs, so no payload is ever tried as one format
    and then guessed as another. Returns the kind and the parsed payload (a list of line
    documents for JSONL).
    """
    try:
        payload = load_json(text)
    except json.JSONDecodeError:
        # The Collector file exporter writes one TracesData object per line; nothing else
        # has a line format. A line that is not JSON re-raises as "not valid JSON".
        documents = [load_json(line) for line in text.splitlines() if line.strip()]
        if _route(documents, root=True, container="JSON Lines") != "otlp":
            raise ValueError(
                "unsupported shape; JSON Lines input is read as an OTLP span export only; "
                + _SHAPES_HELP
            ) from None
        return "otlp", documents
    if isinstance(payload, dict) and "schema_version" in payload:
        return "artifact", payload
    if isinstance(payload, list):
        return _route(payload, root=False, container="list"), payload
    return _route([payload], root=True, container=None), payload


def _select_trace(adapter: Any, trace_id: str | None, *, what: str) -> str:
    if trace_id is not None:
        return trace_id
    ids = adapter.discover()
    if not ids:
        raise ValueError(f"{what} holds no traces: it contains no spans with a trace id")
    if len(ids) != 1:
        raise ValueError(
            f"{what} holds {len(ids)} traces; pass --trace. Found: {', '.join(ids) or 'none'}"
        )
    return ids[0]


def _load_analysis_source(source: str, trace_id: str | None = None) -> NormalizedTrace:
    return _load_analysis_input(source, trace_id).trace


def _load_analysis_input(source: str, trace_id: str | None = None) -> _AnalysisInput:
    """Strictly identify a tracegraph artifact, OTLP span export or Phoenix CLI trace export."""
    try:
        kind, payload = _parse_analysis_text(_read_source(source))
        if kind == "artifact":
            nt = artifact.from_obj(payload)
        elif kind == "otlp":
            from tracegraph.adapters import OTLPSpanAdapter

            # Defaults identical to ingest-otlp: links are causes, error text is kept in the
            # normalized trace, UNSET reads as ok. Reports still apply safe-v1.
            adapter = OTLPSpanAdapter.from_documents(
                payload if isinstance(payload, list) else [payload]
            )
            nt = _normalize_read(adapter.ingest(_select_trace(adapter, trace_id, what="OTLP export")))
        else:
            from tracegraph.adapters import PhoenixExportAdapter

            phoenix = PhoenixExportAdapter(payload)
            nt = _normalize_read(
                phoenix.ingest(_select_trace(phoenix, trace_id, what="Phoenix export"))
            )
        validate_normalized(nt)
        return _AnalysisInput(nt, kind)
    except (*_LOAD_ERRORS, KeyError) as exc:
        raise typer.BadParameter(f"cannot load analysis input {source}: {_load_reason(exc)}") from exc


def _px_run(command: list[str], *, action: str, timeout: int = _PX_TIMEOUT_SECONDS) -> str:
    """Run one read-only ``px`` command without echoing its potentially-sensitive output."""
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise _PxError(
            "Phoenix CLI `px` was not found; install/configure @arizeai/phoenix-cli first"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _PxError(f"{action} timed out after {timeout} seconds") from exc
    except subprocess.CalledProcessError as exc:
        raise _PxError(
            f"{action} failed with exit {exc.returncode}; verify Phoenix endpoint, authentication, "
            "and project configuration"
        ) from exc
    return result.stdout


def _px_floor() -> str:
    return ".".join(map(str, _PX_MIN_VERSION))


def _px_version() -> str:
    text = _px_run(["px", "--version"], action="px version check", timeout=10).strip()
    match = _PX_VERSION_RE.search(text)
    if match is None:
        raise _PxError(f"could not determine the Phoenix CLI version; install px >= {_px_floor()}")
    parsed = tuple(int(part) for part in match.groups())
    if parsed < _PX_MIN_VERSION:
        raise _PxError(
            f"Phoenix CLI {match.group(0)} is too old; install px >= {_px_floor()} with "
            "`npm install -g @arizeai/phoenix-cli@latest`"
        )
    return match.group(0)


def _px_json(command: list[str], *, action: str) -> Any:
    text = _px_run(command, action=action)
    try:
        return load_json(text)
    except ValueError as exc:
        raise _PxError(f"{action} returned invalid JSON") from exc


def _px_project_args(project: str | None) -> list[str]:
    return ["--project", project] if project else []


def _px_read_command(*args: str, project: str | None = None) -> list[str]:
    """A read-only ``px`` command that prints machine-readable JSON and no progress UI."""
    return ["px", *args, "--format", "raw", "--no-progress", *_px_project_args(project)]


def _normalize_read(raw: RawTrace) -> NormalizedTrace:
    """Normalize adapter output, printing its read disclosures to stderr first.

    They describe evidence this read could not interpret, not the run, so they belong next to
    the command that read it rather than in the artifact, which records the run itself.
    """
    for warning in raw.ingest_warnings:
        err_console.print(f"[yellow]warning:[/] {escape(warning)}")
    return normalize(raw)


def _px_normalize(payload: Any, trace_id: str) -> NormalizedTrace:
    try:
        from tracegraph.adapters import PhoenixExportAdapter

        adapter = PhoenixExportAdapter(payload)
        nt = _normalize_read(adapter.ingest(trace_id))
        validate_normalized(nt)
        return nt
    except (ValueError, KeyError) as exc:
        raise _PxError(
            f"px returned an unsupported or incomplete trace export: {_load_reason(exc)}"
        ) from exc


def _px_trace(trace_id: str, *, project: str | None = None) -> NormalizedTrace:
    """Read one annotated trace through ``px`` without persisting its raw body."""
    command = _px_read_command("trace", "get", trace_id, "--include-annotations", project=project)
    payload = _px_json(command, action="px trace get")
    return _px_normalize(payload, trace_id)


def _px_latest_trace(*, project: str | None = None) -> tuple[NormalizedTrace, bool, int]:
    """Select the newest error, or the newest trace when the scan contains no error."""
    command = _px_read_command(
        "trace", "list", "--limit", str(_PX_SCAN_LIMIT), "--include-annotations", project=project
    )
    payload = _px_json(command, action="px trace list")
    if not isinstance(payload, list):
        raise _PxError("px trace list returned an unsupported response; expected a JSON array")
    if not payload:
        raise _PxError("the configured Phoenix project has no traces")

    try:
        from tracegraph.adapters import PhoenixExportAdapter

        adapter = PhoenixExportAdapter(payload)
        ids = adapter.discover()
    except ValueError as exc:
        raise _PxError(
            f"px returned an unsupported or incomplete trace list: {_load_reason(exc)}"
        ) from exc

    statuses: list[str] = []
    for trace in payload:
        # px derives this aggregate from child span statuses (scripts/
        # verify_phoenix_cli_contract.py checks that against the pinned version). The
        # report below independently normalizes those spans, so selection and diagnosis
        # deliberately remain separate checks rather than treating the aggregate as evidence.
        status = trace.get("status")
        if status not in {"OK", "ERROR"}:
            raise _PxError(
                "px trace list returned a trace without the required OK/ERROR status"
            )
        statuses.append(status)

    # The pinned CLI contract test verifies that trace list emits newest-first order.
    error_index = next(
        (
            index for index, status in enumerate(statuses) if status == "ERROR"
        ),
        None,
    )
    selected_index = error_index if error_index is not None else 0
    selected_id = ids[selected_index]
    nt = _px_normalize(payload[selected_index], selected_id)
    return nt, error_index is not None, len(payload)


def _argument_text(value: str) -> str:
    # JSON quoting makes controls visible; nonprinting Unicode (including bidi controls)
    # also uses escapes. Identifiers remain identifiers, not anonymized aliases.
    return json.dumps(value, ensure_ascii=not value.isprintable())


def _print_evidence(text: str) -> None:
    """Keep evidence identifiers and paths literal and copyable, even on narrow terminals."""
    console.print(text, markup=False, highlight=False, emoji=False, soft_wrap=True)


def _show_investigation_command(args: list[str]) -> None:
    if any(not arg.isprintable() for arg in args):
        console.print(
            "  Nonprinting argument: no copyable shell command. Use these escaped argv values:",
            markup=False,
        )
        _print_evidence("  " + json.dumps(args, ensure_ascii=True))
        return
    _print_evidence("  " + shlex.join(args))


_DESCRIPTOR_ALIAS = re.compile(r"^/(dev/(fd/|std(in|out|err)$)|proc/[^/]+(/task/[^/]+)?/fd/)")


def _is_descriptor_alias(source: str) -> bool:
    """Whether ``source`` names a file descriptor of this process rather than a file.

    ``/dev/stdin``, ``/dev/fd/N`` and ``/proc/<pid>/fd/N`` pass ``is_file()`` when the
    descriptor is a regular file, yet another process opening the same path gets its own
    descriptor, so a copied command would not read this input. ``realpath`` cannot tell:
    on Linux ``/proc/self/fd/0`` resolves straight through to the redirected file, and on
    macOS ``/dev/fd/0`` reports a virtual inode unrelated to ``fstat(0)``. So test the
    spelling the loader opens (``Path`` drops ``.`` and trailing slashes, and a directory
    descriptor such as ``/proc/self/fd/N/trace.json`` is an alias too), then resolve the
    directory with ``realpath`` (a symlinked directory into ``/dev/fd`` shows that spelling,
    and ``..`` applies after the link it follows, not before) and follow the final
    component's symlink chain one hop at a time. Hops are bounded like the kernel's limit;
    a loop or a dangling link stops the walk and is left to ``is_file``. Known gap: a symlink
    to a directory descriptor (``link -> /proc/self/fd/N``, then ``link/trace.json``) resolves
    past the alias; closing it means re-implementing realpath's component walk with a hook.
    """
    path = os.path.join(os.getcwd(), source)
    for _ in range(40):
        path = str(Path(path))
        if _DESCRIPTOR_ALIAS.match("/" + path.lstrip("/")):
            return True
        path = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        if _DESCRIPTOR_ALIAS.match(path):
            return True
        try:
            target = os.readlink(path)
        except OSError:
            return False
        path = os.path.join(os.path.dirname(path), target)
    return False

def _render_next_steps(
    report: AnalysisReport,
    *,
    limit: int,
    source: str | None = None,
    source_kind: str | None = None,
    saved_artifact: Path | None = None,
    json_out: Path | None = None,
    project: str | None = None,
) -> None:
    """Called only after requested writes succeed; never saves or executes guidance."""
    if json_out is not None:
        _print_evidence("\nSaved JSON report: " + _argument_text(str(json_out)))
    findings = report.primary_failures[:limit]
    if not findings:
        return
    console.print("\n[bold]Next investigation steps[/]")
    console.print(
        "  IDs identify evidence, not anonymous data. explain may display raw names/errors; "
        "review before sharing. Recorded predecessors do not prove exception propagation."
    )
    if json_out is None and len(report.primary_failures) > limit:
        console.print("  No JSON report saved. Request --json-out with a chosen report path.")

    is_artifact = source_kind == "artifact"
    # Checking a named pipe's type does not open or read it again.
    replay = source is not None and (
        source == "-" or not Path(source).is_file() or _is_descriptor_alias(source)
    )
    target = str(saved_artifact) if saved_artifact is not None else (
        source if is_artifact and not replay else None
    )
    if target is None:
        target = "CHOOSE_NEW_ARTIFACT_PATH"
        console.print(
            "  Template only: replace CHOOSE_NEW_ARTIFACT_PATH with a new, unused path. "
            "No artifact was saved by this diagnosis."
        )
        if source is None:
            args = ["tracegraph", "phoenix", "diagnose"]
            if project is not None:
                args.append("--project=" + project)
            args.extend(["--save-artifact=" + target, "--", report.trace_id])
            console.print("  Re-fetch the selected trace explicitly; its upstream data may have changed. "
                          "If re-fetch changed the trace, diagnose it again before explaining.")
            _show_investigation_command(args)
        elif is_artifact:
            console.print(
                "  Replay the original artifact stream into your chosen file first; "
                "this consumed stream cannot be recovered by tracegraph."
            )
        else:
            ingest, label = (
                ("ingest-otlp", "OTLP") if source_kind == "otlp" else ("ingest-phoenix", "Phoenix")
            )
            if replay:
                console.print(f"  Replay the original {label} export on stdin for this ingest command.")
            _show_investigation_command([
                "tracegraph", ingest, "--file=" + ("-" if replay else source),
                "--trace=" + report.trace_id, "--out=" + target,
            ])
        console.print("  After saving, use the step IDs below.")
    for finding in findings:
        _show_investigation_command([
            "tracegraph", "explain", "--", target, finding.step.step_id,
        ])


def _render_analysis(report: AnalysisReport, *, limit: int) -> None:
    status_style = "red" if report.status == StepStatus.ERROR.value else "green"
    console.print(
        f"[bold {status_style}]{escape(report.trace_id)}[/]  "
        f"{report.step_count} steps · {report.error_count} error"
    )

    console.print("\n[bold]What failed — candidates to investigate[/]")
    if not report.primary_failures:
        console.print("  [green]No error step found.[/]")
    for finding in report.primary_failures[:limit]:
        step = finding.step
        console.print(f"  [red]✗[/] {escape(step.name or step.kind)} ({step.kind}, step {step.seq})")
        _print_evidence(f"    step_id: {_argument_text(step.step_id)}")
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
        console.print(f"  [dim]… {remaining} more failure candidate(s) not shown; "
                      f"use --limit {len(report.primary_failures)} to show all.[/]")
    if report.propagated_failures:
        names = ", ".join(escape(item.name or item.kind) for item in report.propagated_failures)
        console.print(f"  [dim]propagated/context errors: {names}[/]")

    console.print("\n[bold]Repeated behavior[/]")
    if report.patterns:
        for pattern in report.patterns:
            labels = " → ".join(escape(label or "(unnamed)") for label in pattern.labels)
            console.print(f"  • {pattern.pattern_id}@v{pattern.pattern_version}: {labels}")
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
        metric_deltas = comparison.metric_deltas
        wall_delta = metric_deltas["wall_duration_ms"]
        token_delta = metric_deltas["total_tokens"]
        cost_delta = metric_deltas["total_cost"]
        wall_text = f"{wall_delta:+g} ms" if wall_delta is not None else "unavailable"
        token_text = f"{token_delta:+d}" if token_delta is not None else "unavailable"
        cost_text = (
            f"{Decimal(str(cost_delta)):+f} {report.metrics.cost_currency}"
            if cost_delta is not None and report.metrics.cost_currency
            else "unavailable"
        )
        console.print(
            f"  telemetry delta: wall={wall_text} · tokens={token_text} · cost={cost_text}"
        )

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
    raw = _read_artifact_bytes(path)
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    nt = artifact.loads(raw.decode("utf-8"))
    del raw  # only the digest and the parsed trace are retained per file
    validate_normalized(nt)
    return _LoadedArtifact(path=path, trace=nt, digest=digest)


def _load(path: Path) -> NormalizedTrace:
    """Load + validate an explicitly-named artifact, mapping failures to clean CLI errors."""
    try:
        return _load_validated(path)
    except _LOAD_ERRORS as exc:
        raise _load_error(path, exc) from exc


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
                raise _load_error(f, exc) from exc
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
            "--backend ladybug requires the optional agent-tracegraph[cypher] dependency"
        ) from exc
    return LadybugStore


def _store_from_artifact(path: Path, backend: QueryBackend) -> Any:
    # _store_cls may raise its own clean BadParameter (missing ladybug) — keep it outside the
    # try so we only translate *load* failures, not backend-selection ones.
    cls = _store_cls(backend)
    try:
        # Bounded read through the common boundary rather than cls.load_artifact(path),
        # which would re-open the file without the size ceiling.
        nt = artifact.loads(_read_artifact_bytes(path).decode("utf-8"))
        return cls.from_trace(nt)
    except _LOAD_ERRORS as exc:
        raise _load_error(path, exc) from exc


def _store_from_trace(nt: NormalizedTrace, backend: QueryBackend) -> Any:
    cls = _store_cls(backend)
    try:
        return cls.from_trace(nt)
    except _LOAD_ERRORS as exc:
        # A trace another backend accepts can still exceed this one (e.g. INT64 seq).
        raise typer.BadParameter(
            f"cannot load trace {nt.trace.trace_id} into the {backend.value} backend: "
            f"{_load_reason(exc)}"
        ) from exc


def _close_store(store: Any) -> None:
    """Release a backend store's resources (no-op for backends without close())."""
    close = getattr(store, "close", None)
    if close is not None:
        close()


def _close_stores(stores: dict[str, Any]) -> None:
    """Close every store, even when one close raises; re-raise the first failure."""
    first: BaseException | None = None
    for store in stores.values():
        try:
            _close_store(store)
        except BaseException as exc:  # noqa: BLE001 - must keep closing the rest
            if first is None:
                first = exc
    stores.clear()
    if first is not None:
        raise first


def _search_with_backend(
    traces: list[NormalizedTrace],
    pattern,
    backend: QueryBackend,
) -> list[Match]:
    """Match a pattern across traces.

    Never returns a live store: each backend store is opened, queried, and closed
    within this call (including on exceptions), so a directory sweep holds at most
    one open backend DB at a time. A caller that needs stores afterwards (``query
    --explain``) rebuilds them lazily for just the traces it renders."""
    if backend is QueryBackend.MEMORY:
        return pattern_search(traces, pattern)

    matches: list[Match] = []
    fell_back = False
    for nt in traces:
        store = _store_from_trace(nt, backend)
        try:
            steps = nt.steps_by_id()
            for path in store.find_matches(pattern):
                matches.append(match_for(nt.trace.trace_id, steps, path))
            fell_back = fell_back or getattr(store, "fell_back_to_python", False)
        finally:
            _close_store(store)
    if fell_back:
        # Honesty signal (off result stdout): the ladybug accelerator couldn't compile this
        # pattern under LadybugDB's 30-hop cap and ran the pure-Python matcher instead. Results
        # are identical — this just tells the user the accelerator deferred.
        err_console.print(
            "[dim]⚠ pattern not Cypher-compilable (unbounded gap); ran the pure-Python "
            "matcher over the raw causal graph — results are identical to --backend ladybug's.[/]"
        )
    return matches


def _label(step) -> str:
    # escape() both producer-controlled strings. Span names and error text routinely contain
    # square brackets ("tool[0]", "[Errno 2] ..."), which Rich reads as markup: it would strip
    # them from the rendered name, so the operator silently sees a *wrong* name or error
    # rather than a mangled one. Every other render path in this module already escapes.
    base = escape(step.name or step.kind.value)
    suffix = "  ⚠ lossy-projection" if step.projection_lossy else ""
    if step.status is StepStatus.ERROR:
        return f"[bold red]{step.seq}: {base}  ✗ {escape(step.error_msg or 'error')}[/]{suffix}"
    if step.source is StepSource.INTERRUPT:
        # Paused, not finished: green would read as success, which is the misreading the
        # interrupt step exists to prevent.
        return f"[yellow]{step.seq}: {base}  ⏸ interrupted[/]{suffix}"
    if step.source is StepSource.SCHEDULED:
        # Scheduled and unwritten: no outcome at all, so neither green nor the interrupt's
        # yellow, which marks a task that did run up to its pause.
        return f"[dim]{step.seq}: {base}  ◌ scheduled[/]{suffix}"
    return f"[green]{step.seq}: {base}[/]{suffix}"


def _render_tree(nt: NormalizedTrace) -> None:
    children: dict[str, list[str]] = {}
    for e in nt.edges_of(EdgeType.TREE_PARENT):
        children.setdefault(e.dst, []).append(e.src)
    has_parent = {e.src for e in nt.edges_of(EdgeType.TREE_PARENT)}
    steps = nt.steps_by_id()
    # (seq, step_id) — the canonical tie-break used everywhere else. Sorting this *set* by
    # seq alone left equal-seq steps in hash order, so the rendered tree could differ between
    # runs under PYTHONHASHSEED randomization.
    order = sorted(
        children.keys() | {s.step_id for s in nt.steps}, key=lambda i: (steps[i].seq, i)
    )

    def add_subtree(root_node: Tree, rid: str) -> None:
        # Iterative DFS: a recursive walk overflows on deep-linear traces (one super-step
        # per node). Push children in reverse-seq order so they render seq-ascending as they
        # pop, and each child's branch is rendered under its own parent.
        stack: list[tuple[Tree, str]] = [(root_node, rid)]
        while stack:
            rich_parent, nid = stack.pop()
            branch = rich_parent.add(_label(steps[nid]))
            for child in sorted(children.get(nid, []), key=lambda i: (steps[i].seq, i), reverse=True):
                stack.append((branch, child))

    # The header is producer-controlled too: a trace id or source kind containing brackets
    # would raise MarkupError on an otherwise valid artifact, exactly as the step labels did.
    root_tree = Tree(
        f"[bold]{escape(nt.trace.trace_id)}[/] ({escape(nt.trace.source_kind)})"
    )
    for rid in [i for i in order if i not in has_parent]:
        add_subtree(root_tree, rid)
    console.print(root_tree)

    n_err = sum(1 for s in nt.steps if s.status is StepStatus.ERROR)
    n_tool = sum(1 for s in nt.steps if s.kind.value == "TOOL")
    n_lossy = sum(1 for s in nt.steps if s.projection_lossy)
    n_paused = sum(1 for s in nt.steps if s.source is StepSource.INTERRUPT)
    n_scheduled = sum(1 for s in nt.steps if s.source is StepSource.SCHEDULED)
    # Appended only when present, so the summary of every trace without interrupt or
    # scheduled steps stays byte-identical to what earlier releases printed.
    paused = f" · {n_paused} interrupted" if n_paused else ""
    scheduled = f" · {n_scheduled} scheduled" if n_scheduled else ""
    console.print(
        f"\n[dim]{len(nt.steps)} steps · {n_tool} tool · "
        f"{n_err} error · {n_lossy} lossy-projection{paused}{scheduled}[/]"
    )


@app.command()
def ingest(
    sqlite: Path = typer.Option(..., help="Path to a LangGraph SqliteSaver DB file."),
    thread: str = typer.Option(..., "--thread", "-t", help="thread_id to ingest."),
    out: Path = typer.Option(None, "--out", "-o", help="Artifact path (default <thread>.json)."),
    error_channel: str = typer.Option("error", help="State channel that signals a step error."),
    graph: str = typer.Option(
        None,
        "--graph",
        help=(
            "Compiled graph that wrote the thread, as path/to/file.py:attr or pkg.module:attr, "
            "to report tasks it schedules but that recorded nothing. Imports and runs that "
            "module, and the graph's state callbacks while reading; never a node."
        ),
    ),
) -> None:
    """Ingest a LangGraph thread's checkpoint history into a portable artifact."""
    from tracegraph.sqlite_snapshot import ingest_snapshot

    target = out or artifact.default_path(thread)
    _distinct_paths([sqlite], [target])
    # Passed only when given, so an ingest without --graph makes exactly the call it always has.
    extra: dict[str, Any] = {}
    if graph is not None:
        from tracegraph.graph_ref import load_graph

        try:
            extra["graph"] = load_graph(graph)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--graph") from exc
    try:
        nt = _normalize_read(ingest_snapshot(sqlite, thread, error_channel=error_channel, **extra))
        _save_ingested(nt, target, explicit=out is not None)
    except (OSError, ValueError, KeyError) as exc:
        raise typer.BadParameter(f"cannot ingest checkpoint: {_load_reason(exc)}") from exc
    console.print(f"[green]ingested {len(nt.steps)} steps[/] → {escape(str(target))}")


@app.command(name="ingest-otlp")
def ingest_otlp(
    file: str = typer.Option(..., "--file", "-f", help="OTLP JSON/JSONL span export (resourceSpans), or '-' for stdin."),
    trace: str = typer.Option(None, "--trace", "-t", help="traceId to ingest (default: the file's only trace)."),
    out: Path = typer.Option(None, "--out", "-o", help="Artifact path (default <traceId>.json)."),
) -> None:
    """Ingest one trace from an OpenInference/OTLP span export into a portable artifact."""
    from tracegraph.adapters import OTLPSpanAdapter

    try:
        # Bounded read through the common size-limited boundary (from_file would
        # read_text the whole file with no ceiling). KeyError joins the usual load
        # errors: the adapter raises it for an unknown/missing trace id.
        adapter = OTLPSpanAdapter.from_json(_read_source(file))
        trace = _select_trace(adapter, trace, what="file")
        nt = _normalize_read(adapter.ingest(trace))
    except (*_LOAD_ERRORS, KeyError) as exc:
        raise typer.BadParameter(f"cannot ingest OTLP export {file}: {_load_reason(exc)}") from exc
    target = out or artifact.default_path(trace)
    _distinct_paths([Path(file)] if file != "-" else [], [target])
    _save_ingested(nt, target, explicit=out is not None)
    console.print(f"[green]ingested {len(nt.steps)} spans[/] → {escape(str(target))}")


@app.command(name="ingest-phoenix")
def ingest_phoenix(
    file: str = typer.Option(..., "--file", "-f", help="Phoenix CLI trace JSON or '-' for stdin."),
    trace: str = typer.Option(None, "--trace", "-t", help="traceId (required for multi-trace exports)."),
    out: Path = typer.Option(None, "--out", "-o", help="Body-free artifact path."),
) -> None:
    """Ingest one Phoenix CLI trace export into a body-free portable artifact."""
    try:
        payload = load_json(_read_source(file))
        if isinstance(payload, dict) and "schema_version" in payload:
            nt = artifact.from_obj(payload)
        else:
            # Forced adapter: this command names the format, so no shape routing. A document
            # that analyze would call ambiguous is read here as Phoenix, by ingest-otlp as OTLP.
            from tracegraph.adapters import PhoenixExportAdapter

            adapter = PhoenixExportAdapter(payload)
            nt = _normalize_read(adapter.ingest(_select_trace(adapter, trace, what="Phoenix export")))
        validate_normalized(nt)
    except (*_LOAD_ERRORS, KeyError) as exc:
        raise typer.BadParameter(f"cannot load analysis input {file}: {_load_reason(exc)}") from exc
    if nt.trace.source_kind != "phoenix_cli":
        raise typer.BadParameter("--file must contain a Phoenix CLI trace export")
    target = out or artifact.default_path(nt.trace.trace_id)
    _distinct_paths([Path(file)] if file != "-" else [], [target])
    _save_ingested(nt, target, explicit=out is not None)
    console.print(f"[green]ingested {len(nt.steps)} Phoenix spans[/] → {escape(str(target))}")


@app.command(name="analyze")
def analyze_command(
    source: str = typer.Argument(..., help="Tracegraph artifact, OTLP span export (JSON or Collector JSONL), Phoenix CLI export, or '-' for stdin."),
    trace: str = typer.Option(None, "--trace", "-t", help="Trace id for a multi-trace OTLP or Phoenix input."),
    baseline: str = typer.Option(None, "--baseline", help="Explicit artifact, OTLP export, or Phoenix export baseline."),
    baseline_trace: str = typer.Option(None, "--baseline-trace", help="Trace id in a multi-trace OTLP or Phoenix baseline."),
    json_out: Path = typer.Option(None, "--json-out", help="Write deterministic body-free JSON report."),
    limit: int = typer.Option(3, "--limit", "-l", help="Failure candidates to investigate rendered in the terminal."),
) -> None:
    """Automatically diagnose failures, retries, telemetry, and an optional baseline."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer")
    _distinct_paths([Path(p) for p in (source, baseline) if p and p != "-"], [json_out] if json_out else [])
    loaded = _load_analysis_input(source, trace)
    nt = loaded.trace
    baseline_nt = _load_analysis_source(baseline, baseline_trace) if baseline else None
    report = build_analysis(nt, baseline=baseline_nt)
    _render_analysis(report, limit=limit)
    if json_out:
        _save_report(report, json_out)
    _render_next_steps(report, limit=limit, source=source,
                       source_kind=loaded.source, json_out=json_out)


@phoenix_app.command(name="diagnose")
def phoenix_diagnose(
    trace_id: str | None = typer.Argument(
        None, help="Phoenix trace id (default: latest failed trace)."
    ),
    project: str = typer.Option(None, "--project", help="Phoenix project name or id."),
    baseline: str = typer.Option(None, "--baseline", help="Explicit Phoenix baseline trace id."),
    save_artifact: Path = typer.Option(None, "--save-artifact", help="Save the body-free artifact."),
    json_out: Path = typer.Option(None, "--json-out", help="Write deterministic body-free JSON report."),
    limit: int = typer.Option(3, "--limit", "-l", help="Failure candidates to investigate rendered in the terminal."),
) -> None:
    """Diagnose an explicit trace or automatically select a recent failure."""
    if limit <= 0:
        raise typer.BadParameter("--limit must be a positive integer")
    _distinct_paths([], [p for p in (save_artifact, json_out) if p is not None])
    try:
        _px_version()
        if trace_id:
            nt = _px_trace(trace_id, project=project)
        else:
            nt, selected_error, scanned = _px_latest_trace(project=project)
            trace_id = nt.trace.trace_id
            if selected_error:
                console.print(
                    f"[cyan]selected latest failed Phoenix trace[/] {escape(trace_id)}"
                )
            else:
                console.print(
                    f"[yellow]No failed trace found in the latest {scanned}; "
                    f"diagnosing the newest trace[/] {escape(trace_id)}"
                )
        baseline_nt = _px_trace(baseline, project=project) if baseline else None
    except _PxError as exc:
        raise typer.BadParameter(str(exc)) from exc
    report = build_analysis(nt, baseline=baseline_nt)
    _render_analysis(report, limit=limit)
    if save_artifact:
        _save_artifact(nt, save_artifact)
        _print_evidence("\nSaved artifact: " + _argument_text(str(save_artifact)))
    if json_out:
        _save_report(report, json_out)
    _render_next_steps(report, limit=limit, saved_artifact=save_artifact,
                       json_out=json_out, project=project)


@phoenix_app.command(name="doctor")
def phoenix_doctor(
    project: str = typer.Option(None, "--project", help="Phoenix project name or id."),
) -> None:
    """Check the read-only Phoenix CLI, connection, authentication, and project access."""
    try:
        version = _px_version()
        projects = _px_json(
            _px_read_command("project", "list", "--limit", "1"), action="px project list"
        )
        if not isinstance(projects, list):
            raise _PxError("px project list returned an unsupported response; expected a JSON array")
        traces = _px_json(
            _px_read_command("trace", "list", "--limit", "1", project=project),
            action="px trace list",
        )
        if not isinstance(traces, list):
            raise _PxError("px trace list returned an unsupported response; expected a JSON array")
        if traces:
            from tracegraph.adapters import PhoenixExportAdapter

            PhoenixExportAdapter(traces)
    except (ValueError, _PxError) as exc:
        err_console.print(f"[bold red]FAIL[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc

    target = escape(project or "configured project")
    if not traces:
        console.print(
            f"[bold yellow]WARN[/] px {version} can access {target}, but it has no traces yet."
        )
        console.print("  Instrument and run the agent once, then retry `tracegraph phoenix diagnose`.")
        return
    console.print(f"[bold green]READY[/] px {version} can read {target}.")
    console.print("  Run `tracegraph phoenix diagnose` to inspect the latest failed trace.")


@app.command()
def inspect(artifact_path: Path = typer.Argument(..., help="Artifact JSON from `ingest`.")) -> None:
    """Render a trace's derived causal tree."""
    _render_tree(_load(artifact_path))


@app.command()
def validate(
    artifacts: list[Path] = typer.Argument(..., help="Artifact JSON files and/or directories."),
) -> None:
    """Validate artifacts against the canonical schema and causal-graph contract."""
    # Resolve the size-limit configuration ONCE, before the per-file loop: a bad
    # TRACEGRAPH_MAX_ARTIFACT_BYTES is a configuration error and must abort the command,
    # not be reported as "INVALID" against every (perfectly valid) file.
    _max_artifact_bytes()
    files = _artifact_files(artifacts)
    failures = 0
    for path in files:
        try:
            nt = _load(path)
        except typer.BadParameter as exc:
            # _load already narrowed to load-shaped failures (_LOAD_ERRORS); anything else
            # is a real tracegraph bug and must propagate, not print as "INVALID".
            failures += 1
            console.print(f"[bold red]INVALID[/] {escape(str(path))}: {escape(str(exc))}")
            continue
        console.print(
            f"[green]OK[/] {escape(str(path))}  [dim]{escape(nt.trace.trace_id)} · {len(nt.steps)} steps[/]"
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
    try:
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
        result = explain_chain(store, chosen, steps=steps)
    finally:
        _close_store(store)
    target = result.target
    # Unescaped, a name or error containing "[...]" raises MarkupError and kills the command.
    console.print(f"[bold]{escape(target.name or target.kind.value)}[/] (step {target.seq}) "
                  f"\\[{target.status.value}] {escape(target.error_msg or '')}")
    if not result.chain:
        console.print("[dim]no causes (this is a root step)[/]")
    for s in result.chain:
        flag = " [yellow]⚠ projection-lossy[/]" if s.projection_lossy else ""
        console.print(f"  ← {escape(s.name or s.kind.value)} (step {s.seq}){flag}")
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
    console.print(f"[dim]Derived TREE_PARENT comparison; lossy steps: A={sum(s.projection_lossy for s in nt_a.steps)}, B={sum(s.projection_lossy for s in nt_b.steps)}[/]")
    # --structure ignores names when comparing but still prints them, so the reported
    # subtrees can be found in the runs.
    if structure:
        result = tree_diff(nt_a, nt_b, structure_only, display_label=default_label)
    else:
        result = tree_diff(nt_a, nt_b)
    if result.identical:
        console.print("[green]IDENTICAL[/] — the two runs are structurally isomorphic.")
        raise typer.Exit(0)
    console.print("[bold yellow]NOT IDENTICAL[/]")
    for line in result.changes:
        console.print(f"  • {escape(line)}")
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
    matches = _search_with_backend(traces, pattern, backend)
    # --explain stores are built lazily below, AFTER --limit truncation, so a 10-of-500
    # cap opens at most 10 backend DBs regardless of how many traces matched.
    stores: dict[str, Any] = {}
    try:
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
        steps_maps: dict[str, dict] = {}
        for m in matches:
            console.print(
                f"[green]{escape(m.trace_id)}[/]: "
                + " → ".join(escape(label) for label in m.labels)
            )
            if explain:
                store = stores.get(m.trace_id)
                if store is None:
                    store = _store_from_trace(traces_by_id[m.trace_id], backend)
                    stores[m.trace_id] = store
                steps_map = steps_maps.get(m.trace_id)
                if steps_map is None:
                    steps_map = traces_by_id[m.trace_id].steps_by_id()
                    steps_maps[m.trace_id] = steps_map
                # The effect (last step in the matched path) is what we trace back from —
                # the rest of the match is by construction part of its causal chain, but
                # explain() surfaces every cause including ones the pattern didn't constrain.
                result = explain_chain(store, m.step_ids[-1], steps=steps_map)
                if not result.chain:
                    console.print("    [dim]← (no causes — matched effect is a root step)[/]")
                for s in result.chain:
                    flag = " [yellow]⚠ projection-lossy[/]" if s.projection_lossy else ""
                    # Same fallback label as `tracegraph explain` and the tree renderers.
                    console.print(f"    ← {escape(s.name or s.kind.value)} (step {s.seq}){flag}")
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
    finally:
        _close_stores(stores)


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
    matches = _search_with_backend(traces, pattern, backend)
    traces_by_id = {item.trace.trace.trace_id: item.trace for item in loaded}
    digests_by_id = {item.trace.trace.trace_id: item.digest for item in loaded}
    try:
        report = build_candidate_report(pattern, matches, traces_by_id, digests_by_id)
        save_candidate_report(report, out)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"cannot export review candidates: {exc}") from exc
    console.print(
        f"[green]exported {len(report.candidates)} review candidate(s)[/] "
        f"from {len(matches)} match(es) → {escape(str(out))}"
    )


@app.command(name="export-incident-memo")
def export_incident_memo(
    artifact_path: Path = typer.Argument(..., help="Path to artifact JSON file."),
    out: Path = typer.Option(..., "--out", "-o", help="Destination markdown memo file path."),
) -> None:
    """Export a strict-allowlist incident post-mortem memo for memtomem LTM."""
    _distinct_paths([artifact_path], [out])
    try:
        loaded = _load_artifact_evidence(artifact_path)
    except _LOAD_ERRORS as exc:
        raise _load_error(artifact_path, exc) from exc
    from tracegraph.analysis.memo import build_incident_memo, save_incident_memo

    memo_content = build_incident_memo(loaded.trace, loaded.digest)
    try:
        save_incident_memo(memo_content, out)
    except OSError as exc:
        raise typer.BadParameter(f"cannot save incident memo: {_load_reason(exc)}") from exc
    console.print(f"[green]exported incident memo[/] → {escape(str(out))}")


def main() -> None:
    app()


def _distinct_paths(inputs: list[Path], outputs: list[Path]) -> None:
    seen = list(inputs)
    for output in outputs:
        for previous in seen:
            if output.resolve() == previous.resolve() or (
                output.exists() and previous.exists() and output.samefile(previous)
            ):
                raise typer.BadParameter("output must not overwrite an input or another output")
        seen.append(output)


def _save_report(report: AnalysisReport, target: Path) -> None:
    """Write a JSON report, turning an unwritable destination into a clean usage error.

    The analysis has already run and been rendered by the time we get here, so an
    unwrapped OSError would replace a completed result with a raw traceback.
    """
    try:
        save_analysis_report(report, target)
    except OSError as exc:
        raise typer.BadParameter(f"cannot write report: {_load_reason(exc)}") from exc


def _save_artifact(nt: NormalizedTrace, target: Path) -> None:
    """Write an artifact to an explicit destination, with the same clean-error contract."""
    try:
        artifact.save_atomic(nt, target)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"cannot save artifact: {_load_reason(exc)}") from exc


def _save_ingested(nt: NormalizedTrace, target: Path, *, explicit: bool) -> None:
    try:
        artifact.save_atomic(nt, target, replace=explicit)
    except FileExistsError as exc:
        raise typer.BadParameter("default output exists; pass --out to explicitly replace a file") from exc
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"cannot save artifact: {_load_reason(exc)}") from exc


if __name__ == "__main__":
    main()
