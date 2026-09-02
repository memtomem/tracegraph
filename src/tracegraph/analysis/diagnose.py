"""Deterministic, body-free diagnosis for one normalized trace."""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tempfile

from pydantic import BaseModel, Field

from tracegraph import artifact
from tracegraph.analysis.ahu import diff as tree_diff
from tracegraph.analysis.patterns import PRESETS, build_index, find_matches
from tracegraph.model import CausalFidelity, EdgeType, NormalizedTrace, Step, StepStatus

REPORT_SCHEMA_VERSION = 2
_AUTO_PRESETS = (
    "tool-retry-failure",
    "repeated-agent-failure",
    "gate-failure-after-success",
    "timeout",
    "plan-then-tool-failure",
    "tool-failure",
    "error",
)


class DiagnosticStep(BaseModel):
    step_id: str
    seq: int
    name: str | None
    kind: str
    status: str


class DiagnosticEdge(BaseModel):
    effect: str
    cause: str
    origin: str | None


class FailureFinding(BaseModel):
    step: DiagnosticStep
    causal_steps: list[DiagnosticStep] = Field(default_factory=list)
    causal_edges: list[DiagnosticEdge] = Field(default_factory=list)


class PatternFinding(BaseModel):
    pattern_id: str
    pattern_version: int
    step_ids: list[str]
    labels: list[str | None]


class EvaluationFinding(BaseModel):
    step_id: str
    name: str
    label: str | None = None
    score: float | None = None


class MetricSummary(BaseModel):
    wall_duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    total_cost: str | None = None
    cost_currency: str | None = None
    evaluations: list[EvaluationFinding] = Field(default_factory=list)


class DecisionEvidenceFinding(BaseModel):
    source: str
    artifact_digest: str
    graph_generation: int
    verdict: str


class BehaviorChange(BaseModel):
    logical_step_key: str
    name: str | None
    before: str | None
    after: str | None


class ComparisonSummary(BaseModel):
    baseline_digest: str
    topology_identical: bool
    topology_changes: list[str]
    behavior_changes: list[BehaviorChange]
    pattern_count_deltas: dict[str, int]
    metric_deltas: dict[str, float | int | str | None]


class AnalysisReport(BaseModel):
    schema_version: int = REPORT_SCHEMA_VERSION
    kind: str = "tracegraph.analysis"
    artifact_digest: str
    trace_id: str
    source_kind: str
    status: str
    causal_fidelity: str
    links_preserved: bool | None
    step_count: int
    error_count: int
    primary_failures: list[FailureFinding]
    propagated_failures: list[DiagnosticStep]
    patterns: list[PatternFinding]
    metrics: MetricSummary
    decision_evidence: list[DecisionEvidenceFinding] = Field(default_factory=list)
    comparison: ComparisonSummary | None = None
    warnings: list[str] = Field(default_factory=list)
    privacy_profile: str = "safe-v1"


def _shown(step: Step) -> DiagnosticStep:
    return DiagnosticStep(
        step_id=step.step_id,
        seq=step.seq,
        name=step.name,
        kind=step.kind.value,
        status=step.status.value,
    )


def _cause_adjacency(nt: NormalizedTrace) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for edge in nt.edges_of(EdgeType.CAUSED_BY):
        out.setdefault(edge.src, []).append(edge.dst)
    steps = nt.steps_by_id()
    for causes in out.values():
        causes.sort(key=lambda sid: (steps[sid].seq, sid))
    return out


def _ancestors(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(adjacency.get(start, []))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(adjacency.get(current, []))
    return seen


def _failures(nt: NormalizedTrace) -> tuple[list[FailureFinding], list[DiagnosticStep]]:
    steps = nt.steps_by_id()
    errors = {step.step_id for step in nt.steps if step.status is StepStatus.ERROR}
    adjacency = _cause_adjacency(nt)
    # Primary vs propagated in ONE pass: an error is propagated iff any proper causal
    # ancestor also errored. cause.seq < effect.seq (validate_raw guarantees it), so
    # walking steps in ascending seq computes each node's flag after all its causes' —
    # O(V+E) total instead of a full ancestor BFS per error.
    has_error_ancestor: dict[str, bool] = {}
    for step in sorted(nt.steps, key=lambda s: (s.seq, s.step_id)):
        has_error_ancestor[step.step_id] = any(
            cause in errors or has_error_ancestor[cause]
            for cause in adjacency.get(step.step_id, [])
        )
    primary_ids = sorted(
        (sid for sid in errors if not has_error_ancestor[sid]),
        key=lambda sid: (steps[sid].seq, sid),
    )
    propagated_ids = sorted(errors - set(primary_ids), key=lambda sid: (steps[sid].seq, sid))

    # Edges indexed by effect: each finding then touches only its own nodes' edges
    # (output-proportional) instead of re-scanning the whole edge list per finding.
    edges_by_effect: dict[str, list] = {}
    for edge in nt.edges_of(EdgeType.CAUSED_BY):
        edges_by_effect.setdefault(edge.src, []).append(edge)

    findings: list[FailureFinding] = []
    for step_id in primary_ids:
        # Full ancestor sets are materialized only for primary failures (the findings
        # need them); propagated errors never pay a BFS.
        ancestor_ids = _ancestors(step_id, adjacency)
        ordered = sorted(ancestor_ids, key=lambda sid: (steps[sid].seq, sid))
        relevant = ancestor_ids | {step_id}
        selected = [
            edge
            for sid in relevant
            for edge in edges_by_effect.get(sid, [])
            if edge.dst in relevant
        ]
        selected.sort(
            key=lambda edge: (steps[edge.src].seq, steps[edge.dst].seq, (edge.src, edge.dst))
        )
        edges = [
            DiagnosticEdge(
                effect=edge.src,
                cause=edge.dst,
                origin=edge.origin.value if edge.origin else None,
            )
            for edge in selected
        ]
        findings.append(
            FailureFinding(
                step=_shown(steps[step_id]),
                causal_steps=[_shown(steps[sid]) for sid in ordered],
                causal_edges=edges,
            )
        )
    return findings, [_shown(steps[sid]) for sid in propagated_ids]


def _patterns(nt: NormalizedTrace) -> list[PatternFinding]:
    index = build_index(nt)
    steps = index[0]
    seen: set[tuple[str, ...]] = set()
    findings: list[PatternFinding] = []
    for name in _AUTO_PRESETS:
        pattern = PRESETS[name]
        for match in find_matches(nt, pattern, index=index):
            key = tuple(match)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                PatternFinding(
                    pattern_id=name,
                    pattern_version=pattern.pattern_version or 1,
                    step_ids=list(match),
                    labels=[steps[sid].name for sid in match],
                )
            )
    return findings


def _iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _metrics(nt: NormalizedTrace) -> MetricSummary:
    starts = [_iso(step.ts) for step in nt.steps]
    ends = [_iso(step.evidence.end_ts) for step in nt.steps if step.evidence]
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    wall: float | None = None
    if starts and ends:
        wall = round((max(ends) - min(starts)).total_seconds() * 1000, 6)

    evidence_items = [step.evidence for step in nt.steps if step.evidence]

    def total_int(field: str) -> int | None:
        values = [
            getattr(item, field)
            for item in evidence_items
            if getattr(item, field) is not None
        ]
        return sum(values) if values else None

    costs: list[Decimal] = []
    cost_currencies: set[str] = set()
    has_currencyless_cost = False
    for evidence in evidence_items:
        if evidence.total_cost is not None:
            try:
                parsed = Decimal(evidence.total_cost)
                if parsed.is_finite() and parsed >= 0:
                    costs.append(parsed)
                    if evidence.cost_currency:
                        cost_currencies.add(evidence.cost_currency)
                    else:
                        has_currencyless_cost = True
            except InvalidOperation:
                pass
    ambiguous_cost = len(cost_currencies) > 1 or (
        bool(cost_currencies) and has_currencyless_cost
    )
    evaluations = [
        EvaluationFinding(
            step_id=step.step_id,
            name=item.name,
            label=item.label,
            score=item.score,
        )
        for step in nt.steps
        if step.evidence
        for item in step.evidence.evaluations
    ]
    evaluations.sort(key=lambda item: (item.step_id, item.name, item.label or ""))
    return MetricSummary(
        wall_duration_ms=wall,
        prompt_tokens=total_int("prompt_tokens"),
        completion_tokens=total_int("completion_tokens"),
        total_tokens=total_int("total_tokens"),
        total_cost=(format(sum(costs), "f") if costs and not ambiguous_cost else None),
        cost_currency=(
            next(iter(cost_currencies))
            if len(cost_currencies) == 1 and not ambiguous_cost
            else None
        ),
        evaluations=evaluations,
    )


def _logical_keys(nt: NormalizedTrace) -> dict[str, Step]:
    steps = nt.steps_by_id()
    parent = {edge.src: edge.dst for edge in nt.edges_of(EdgeType.TREE_PARENT)}
    children: dict[str | None, list[str]] = {}
    for step in nt.steps:
        children.setdefault(parent.get(step.step_id), []).append(step.step_id)
    for ids in children.values():
        ids.sort(key=lambda sid: (steps[sid].seq, sid))

    # Occurrence index among same-(kind, name) siblings, in the sorted sibling order —
    # precomputed with running counters instead of a per-step sibling rescan.
    occurrence_of: dict[str, int] = {}
    for ids in children.values():
        counts: dict[tuple[object, object], int] = {}
        for sid in ids:
            key = (steps[sid].kind, steps[sid].name)
            occurrence_of[sid] = counts.get(key, 0)
            counts[key] = occurrence_of[sid] + 1

    result: dict[str, Step] = {}
    queue: deque[tuple[str, str]] = deque()
    for root in children.get(None, []):
        queue.append((root, ""))
    while queue:
        step_id, prefix = queue.popleft()
        step = steps[step_id]
        label = f"{step.kind.value}:{step.name or '-'}#{occurrence_of[step_id]}"
        key = f"{prefix}/{label}" if prefix else label
        result[key] = step
        for child in children.get(step_id, []):
            queue.append((child, key))
    return result


def _delta(after: float | int | None, before: float | int | None) -> float | int | None:
    return after - before if after is not None and before is not None else None


def _comparison(
    current: NormalizedTrace,
    baseline: NormalizedTrace,
    *,
    current_patterns: list[PatternFinding],
    current_metrics: MetricSummary,
) -> ComparisonSummary:
    structural = tree_diff(baseline, current)
    before, after = _logical_keys(baseline), _logical_keys(current)
    changes: list[BehaviorChange] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        old_status = old.status.value if old else None
        new_status = new.status.value if new else None
        if old_status != new_status:
            changes.append(
                BehaviorChange(
                    logical_step_key=key,
                    name=(new or old).name if (new or old) else None,
                    before=old_status,
                    after=new_status,
                )
            )

    before_patterns = _patterns(baseline)
    after_patterns = current_patterns
    pattern_ids = set(_AUTO_PRESETS)
    deltas = {
        name: sum(item.pattern_id == name for item in after_patterns)
        - sum(item.pattern_id == name for item in before_patterns)
        for name in sorted(pattern_ids)
    }
    bm, cm = _metrics(baseline), current_metrics
    cost_delta: str | None = None
    if (
        bm.total_cost is not None
        and cm.total_cost is not None
        and bm.cost_currency is not None
        and bm.cost_currency == cm.cost_currency
    ):
        cost_delta = format(Decimal(cm.total_cost) - Decimal(bm.total_cost), "f")
    return ComparisonSummary(
        baseline_digest=_digest(baseline),
        topology_identical=structural.identical,
        topology_changes=structural.changes,
        behavior_changes=changes,
        pattern_count_deltas=deltas,
        metric_deltas={
            "wall_duration_ms": _delta(cm.wall_duration_ms, bm.wall_duration_ms),
            "prompt_tokens": _delta(cm.prompt_tokens, bm.prompt_tokens),
            "completion_tokens": _delta(cm.completion_tokens, bm.completion_tokens),
            "total_tokens": _delta(cm.total_tokens, bm.total_tokens),
            "total_cost": cost_delta,
        },
    )


def _digest(nt: NormalizedTrace) -> str:
    return "sha256:" + hashlib.sha256(artifact.dumps(nt).encode("utf-8")).hexdigest()


def analyze(nt: NormalizedTrace, *, baseline: NormalizedTrace | None = None) -> AnalysisReport:
    primary, propagated = _failures(nt)
    patterns = _patterns(nt)
    metrics = _metrics(nt)
    warnings: list[str] = []
    if nt.trace.causal_fidelity is CausalFidelity.PARENT_ONLY:
        warnings.append(
            "Phoenix export preserves parent relationships only; additional fan-in causes may be missing."
        )
    return AnalysisReport(
        artifact_digest=_digest(nt),
        trace_id=nt.trace.trace_id,
        source_kind=nt.trace.source_kind,
        status=nt.trace.status.value,
        causal_fidelity=nt.trace.causal_fidelity.value,
        links_preserved=nt.trace.links_preserved,
        step_count=len(nt.steps),
        error_count=sum(step.status is StepStatus.ERROR for step in nt.steps),
        primary_failures=primary,
        propagated_failures=propagated,
        patterns=patterns,
        metrics=metrics,
        decision_evidence=[
            DecisionEvidenceFinding(**item.model_dump()) for item in nt.trace.decision_evidence
        ],
        comparison=(
            _comparison(nt, baseline, current_patterns=patterns, current_metrics=metrics)
            if baseline
            else None
        ),
        warnings=warnings,
    )


def dumps(report: AnalysisReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def save_atomic(report: AnalysisReport, path: str | Path) -> None:
    target = Path(path)
    text = dumps(report)
    # Round-trip the public model before replacing an existing report (validated on the
    # parsed shape directly — no need to re-parse the JSON we just produced).
    AnalysisReport.model_validate(report.model_dump(mode="json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, target)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
