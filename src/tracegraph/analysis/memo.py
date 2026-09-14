"""memtomem-compatible post-hoc incident memo exporter with strict output allowlist."""

from __future__ import annotations

from collections import deque
import hashlib
import os
from pathlib import Path
import tempfile
import re

from tracegraph.analysis.diagnose import _patterns
from tracegraph.analysis.patterns import PRESETS
from tracegraph.model import (
    CausalFidelity,
    Edge,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    StepStatus,
)
from tracegraph.normalize import validate_structure

INCIDENT_FIDELITY_WARNING = (
    "Failure candidates are investigation leads. Containment/unknown edges do not "
    "prove propagation; explicit causal ancestry is context, not proof of exception propagation."
)
PARENT_ONLY_FIDELITY_WARNING = (
    "Phoenix export preserves parent relationships only; additional fan-in causes may be missing."
)
LANGGRAPH_FIDELITY_WARNING = (
    "LangGraph failure capture covers the configured error channel and task pending writes "
    "for recognized checkpoint layouts; absence of an observed failure does not prove execution success."
)
LINKS_PRESERVED_FIDELITY_WARNING = (
    "Link preservation covers valid in-trace links only; foreign or unresolved links are omitted."
)


def compute_run_digest(run_id: str | None) -> str | None:
    """Deterministic, one-way hash alias for run_id; raw string is never exported."""
    if not run_id:
        return None
    return f"sha256:{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:16]}"


def _format_origin(origin: EdgeOrigin | None) -> str:
    if origin is EdgeOrigin.SPAN_PARENT_FALLBACK:
        return "origin: span_parent_fallback (containment only)"
    if origin is EdgeOrigin.LEGACY_UNKNOWN:
        return "origin: legacy_unknown (unknown evidence)"
    if origin is None:
        return "origin: unknown (missing)"
    return f"origin: {origin.value}"


def _relation_phrase(origin: EdgeOrigin | None) -> str:
    if origin in {EdgeOrigin.CHECKPOINT_PARENT, EdgeOrigin.GRAPH_PARENT, EdgeOrigin.SPAN_LINK}:
        return "caused by"
    if origin is EdgeOrigin.SPAN_PARENT_FALLBACK:
        return "preceded by (containment only)"
    return "preceded by (unknown evidence)"


def build_incident_memo(
    nt: NormalizedTrace,
    artifact_digest: str,
) -> str:
    """Render a strict-allowlist incident memo in Markdown for memtomem LTM.

    Guarantees:
    - Never emits raw run_id (replaces with run_digest hash alias).
    - Never emits raw step names or structural IDs (uses synthetic aliases and StepKind).
    - Never emits raw error_msg, evaluation labels, currency, or payloads.
    - Preserves allowlisted causal fidelity metadata and standard disclosures.
    - Emits actual effect -> cause pairs with edge origins; never fabricates linear paths for branching.
    - Never misrepresents containment or unverified edges as causation.
    - Validates digest format and restricts patterns to known supported presets.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise ValueError(
            f"Invalid artifact_digest: {artifact_digest!r}; must match 'sha256:<64 lowercase hex digits>'"
        )

    validate_structure(nt)
    raw_patterns = _patterns(nt)
    patterns = [
        p for p in raw_patterns
        if p.pattern_id in PRESETS and isinstance(p.pattern_version, int)
    ]

    run_digest = compute_run_digest(nt.trace.run_id)
    short_digest = artifact_digest.removeprefix("sha256:")[:12]
    memo_name = f"incident_{short_digest}"

    # Build synthetic aliases for steps strictly ordered by sequence:
    # e.g. step_1_chain, step_2_tool
    steps_sorted = sorted(nt.steps, key=lambda s: (s.seq, s.step_id))
    alias_by_id: dict[str, str] = {
        s.step_id: f"step_{idx + 1}_{s.kind.value.lower()}"
        for idx, s in enumerate(steps_sorted)
    }

    # Collect raw CAUSED_BY edges grouped by effect
    caused_by_edges = nt.edges_of(EdgeType.CAUSED_BY)
    incoming_edges: dict[str, list[Edge]] = {}
    for edge in caused_by_edges:
        incoming_edges.setdefault(edge.src, []).append(edge)

    error_steps = [s for s in steps_sorted if s.status is StepStatus.ERROR]

    lines: list[str] = [
        "---",
        f"name: {memo_name}",
        f"description: Tracegraph incident post-mortem for artifact {artifact_digest[:23]}...",
        "type: incident",
        "metadata:",
        "  node_type: memory",
        "  type: incident",
        f"  artifact_digest: {artifact_digest}",
        f"  causal_fidelity: {nt.trace.causal_fidelity.value}",
        f"  links_preserved: {str(nt.trace.links_preserved).lower() if nt.trace.links_preserved is not None else 'null'}",
    ]
    if run_digest is not None:
        lines.append(f"  run_digest: {run_digest}")
    lines.extend([
        "---",
        "",
        f"# Incident Post-Mortem: `{memo_name}`",
        "",
        "## Summary",
        f"- **Trace Status**: `{nt.trace.status.value}`",
        f"- **Artifact Digest**: `{artifact_digest}`",
        f"- **Causal Fidelity**: `{nt.trace.causal_fidelity.value}`",
        f"- **Links Preserved**: `{nt.trace.links_preserved}`",
    ])
    if run_digest is not None:
        lines.append(f"- **Run Digest**: `{run_digest}`")
    lines.extend([
        f"- **Step Count**: {len(nt.steps)} (Errors: {len(error_steps)})",
        "",
        "## Diagnostic Findings",
    ])

    if patterns:
        lines.append("### Detected Patterns")
        for p in patterns:
            matched_aliases = [alias_by_id[sid] for sid in p.step_ids if sid in alias_by_id]
            lines.append(f"- **{p.pattern_id}** (v{p.pattern_version}): steps `[{', '.join(matched_aliases)}]`")
        lines.append("")

    if error_steps:
        lines.append("### Failure Causality (Actual Recorded Predecessors)")
        for err_step in error_steps:
            target_alias = alias_by_id[err_step.step_id]
            lines.append(f"- Failure: `{target_alias}` (status: error)")
            direct_edges = incoming_edges.get(err_step.step_id, [])
            if direct_edges:
                pred_items = [
                    f"`{alias_by_id.get(edge.dst, 'unknown_step')}` [{_format_origin(edge.origin)}]"
                    for edge in direct_edges
                ]
                lines.append(f"  - Direct recorded predecessors: {', '.join(pred_items)}")
            else:
                lines.append("  - (no recorded predecessors)")
        lines.append("")

        # Collect unique upstream causal edges reachable from any failure step
        error_step_ids = {s.step_id for s in error_steps}
        reachable_step_ids: set[str] = set(error_step_ids)
        queue: deque[str] = deque(error_step_ids)

        while queue:
            curr_id = queue.popleft()
            for edge in incoming_edges.get(curr_id, []):
                if edge.dst not in reachable_step_ids:
                    reachable_step_ids.add(edge.dst)
                    queue.append(edge.dst)

        ancestry_edges = [
            edge for edge in caused_by_edges
            if edge.src in reachable_step_ids
        ]

        if ancestry_edges:
            lines.append("### Causal Ancestry Graph")
            for edge in ancestry_edges:
                eff_alias = alias_by_id.get(edge.src, "unknown_step")
                cause_alias = alias_by_id.get(edge.dst, "unknown_step")
                rel = _relation_phrase(edge.origin)
                origin_str = f" [{_format_origin(edge.origin)}]"
                lines.append(f"- `{eff_alias}` {rel} `{cause_alias}`{origin_str}")
            lines.append("")
    else:
        lines.append("No step-level errors detected.\n")

    disclosures: list[str] = []
    if error_steps:
        disclosures.append(INCIDENT_FIDELITY_WARNING)
    if nt.trace.causal_fidelity is CausalFidelity.PARENT_ONLY:
        disclosures.append(PARENT_ONLY_FIDELITY_WARNING)
    if nt.trace.source_kind == "langgraph":
        disclosures.append(LANGGRAPH_FIDELITY_WARNING)
    if nt.trace.links_preserved:
        disclosures.append(LINKS_PRESERVED_FIDELITY_WARNING)

    lines.append("## Fidelity Disclosures")
    for d in disclosures:
        lines.append(f"- {d}")
    lines.append("")

    return "\n".join(lines)


def save_incident_memo(content: str, destination: Path) -> None:
    """Atomically write incident memo markdown to destination without leaving orphaned temp files."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as tmp:
            temp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_name, destination)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
