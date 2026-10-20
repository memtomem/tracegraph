"""memtomem-compatible post-hoc incident memo exporter with strict output allowlist."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tracegraph._atomic_write import write_text_atomic
from tracegraph.analysis.diagnose import (
    HEURISTIC_PARENT_FIDELITY_WARNING,
    INCIDENT_FIDELITY_WARNING,
    LANGGRAPH_FIDELITY_WARNING,
    LINKS_PRESERVED_FIDELITY_WARNING,
    PARENT_ONLY_FIDELITY_WARNING,
    has_heuristic_edges,
    pattern_findings,
)
from tracegraph.model import (
    EXPLICIT_ORIGINS,
    SHA256_DIGEST,
    CausalFidelity,
    EdgeOrigin,
    EdgeType,
    NormalizedTrace,
    StepStatus,
    _validated_copy,
)
from tracegraph.normalize import ancestor_ids, incoming_edges_by_step, validate_structure

def compute_run_digest(run_id: str | None) -> str | None:
    """Deterministic, one-way hash alias for run_id; raw string is never exported."""
    if not run_id:
        return None
    # surrogatepass: a run_id from JSON can hold a lone surrogate; other ids hash as before.
    return f"sha256:{hashlib.sha256(run_id.encode('utf-8', 'surrogatepass')).hexdigest()[:16]}"


def _format_origin(origin: EdgeOrigin | None) -> str:
    if origin is EdgeOrigin.SPAN_PARENT_FALLBACK:
        return "origin: span_parent_fallback (containment only)"
    if origin is EdgeOrigin.LEGACY_UNKNOWN:
        return "origin: legacy_unknown (unknown evidence)"
    if origin is None:
        return "origin: unknown (missing)"
    return f"origin: {origin.value}"


def _relation_phrase(origin: EdgeOrigin | None, *, timed_graph_parents: bool) -> str:
    # The capture record says a graph parent was chosen by start order somewhere in the trace,
    # not which one, so no graph-parent edge there is presented as a declared cause.
    if origin is EdgeOrigin.GRAPH_PARENT and timed_graph_parents:
        return "preceded by (graph parent, possibly chosen heuristically)"
    if origin in EXPLICIT_ORIGINS:
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
    if not SHA256_DIGEST.fullmatch(artifact_digest):
        raise ValueError(
            f"Invalid artifact_digest: {artifact_digest!r}; must match 'sha256:<64 lowercase hex digits>'"
        )

    nt = _validated_copy(nt)
    validate_structure(nt)
    patterns = pattern_findings(nt)

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
    incoming_edges = incoming_edges_by_step(caused_by_edges)

    error_steps = [s for s in steps_sorted if s.status is StepStatus.ERROR]
    preserved = nt.trace.links_preserved
    links_preserved = "null" if preserved is None else str(preserved).lower()

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
        f"  links_preserved: {links_preserved}",
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
        f"- **Links Preserved**: `{links_preserved}`",
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
            matched_aliases = [alias_by_id[sid] for sid in p.step_ids]
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
                    f"`{alias_by_id[edge.dst]}` [{_format_origin(edge.origin)}]"
                    for edge in direct_edges
                ]
                lines.append(f"  - Direct recorded predecessors: {', '.join(pred_items)}")
            else:
                lines.append("  - (no recorded predecessors)")
        lines.append("")

        # Collect unique upstream causal edges reachable from any failure step
        error_step_ids = {s.step_id for s in error_steps}
        causes = {sid: [e.dst for e in edges] for sid, edges in incoming_edges.items()}
        reachable_step_ids = error_step_ids | ancestor_ids(error_step_ids, causes)

        ancestry_edges = [
            edge for edge in caused_by_edges
            if edge.src in reachable_step_ids
        ]

        capture = nt.trace.capture
        timed_graph_parents = capture is not None and any(
            gap.code == "otlp.graph_parent_by_timing" for gap in capture.gaps
        )
        if ancestry_edges:
            lines.append("### Causal Ancestry Graph")
            for edge in ancestry_edges:
                eff_alias = alias_by_id[edge.src]
                cause_alias = alias_by_id[edge.dst]
                rel = _relation_phrase(edge.origin, timed_graph_parents=timed_graph_parents)
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
    if has_heuristic_edges(nt):
        disclosures.append(HEURISTIC_PARENT_FIDELITY_WARNING)

    lines.append("## Fidelity Disclosures")
    for d in disclosures:
        lines.append(f"- {d}")
    lines.append("")

    return "\n".join(lines)


def save_incident_memo(content: str, destination: Path) -> None:
    """Atomically write incident memo markdown to destination without leaving orphaned temp files."""
    write_text_atomic(destination, content)
