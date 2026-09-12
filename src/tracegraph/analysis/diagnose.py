"""Deterministic, body-free diagnosis for one normalized trace."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tempfile

from pydantic import BaseModel, Field

from tracegraph import artifact
from tracegraph.analysis.ahu import diff as tree_diff
from tracegraph.analysis.patterns import PRESETS, build_index, find_matches
from tracegraph.model import CausalFidelity, EdgeOrigin, EdgeType, NormalizedTrace, Step, StepStatus
from tracegraph.normalize import validate_structure

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


#: The exact shape ``_safe`` emits, used to detect aliases that reached the final report.
_ALIAS = re.compile(r"redacted:[0-9a-f]{64}")


def _safe(value: str | None) -> str | None:
    if value is None or re.fullmatch(r"[A-Za-z0-9_.:/#@-]{1,200}", value):
        return value
    return "redacted:" + hashlib.sha256(value.encode()).hexdigest()


def _display_name(step: Step) -> str:
    return _safe(step.name or step.kind.value)


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
    explicit = {}
    depth = {}
    for edge in nt.edges_of(EdgeType.CAUSED_BY):
        if edge.origin in {EdgeOrigin.CHECKPOINT_PARENT, EdgeOrigin.GRAPH_PARENT, EdgeOrigin.SPAN_LINK}:
            explicit.setdefault(edge.src, []).append(edge.dst)
    for step in sorted(nt.steps, key=lambda s: (s.seq, s.step_id)):
        depth[step.step_id] = 1 + max((depth[c] for c in adjacency.get(step.step_id, [])), default=-1)
    # Candidate vs context in one pass: only explicit-origin paths establish
    # error ancestry. Containment or legacy/unknown paths cannot suppress candidates. cause.seq < effect.seq (validate_raw guarantees it), so
    # walking steps in ascending seq computes each node's flag after all its causes' —
    # O(V+E) total instead of a full ancestor BFS per error.
    has_error_ancestor: dict[str, bool] = {}
    for step in sorted(nt.steps, key=lambda s: (s.seq, s.step_id)):
        has_error_ancestor[step.step_id] = any(
            cause in errors or has_error_ancestor[cause]
            for cause in explicit.get(step.step_id, [])
        )
    primary_ids = sorted(
        (sid for sid in errors if not has_error_ancestor[sid]),
        key=lambda sid: (-depth[sid], steps[sid].seq, sid),
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
    """Every auto preset's matches, de-duplicated *within* a preset only.

    The key is ``(pattern_id, match)``: two different presets legitimately describe the same
    steps from different angles (a failing tool step is both ``tool-failure`` and ``error``),
    and suppressing the later one would silently drop a finding *and* skew
    ``pattern_count_deltas`` — the delta for a preset would depend on whether an unrelated
    preset happened to match the same tuple first. A single preset yielding the same match
    twice is still collapsed.
    """
    index = build_index(nt)
    steps = index[0]
    seen: set[tuple[str, tuple[str, ...]]] = set()
    findings: list[PatternFinding] = []
    for name in _AUTO_PRESETS:
        pattern = PRESETS[name]
        for match in find_matches(nt, pattern, index=index):
            key = (name, tuple(match))
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
    """Metric summary only; see :func:`_metrics_with_notes` for the disclosure notes."""
    return _metrics_with_notes(nt)[0]


def _metrics_with_notes(nt: NormalizedTrace) -> tuple[MetricSummary, list[str], bool]:
    """Summarize observed metrics and report what had to be withheld.

    Every degradation is disclosed rather than absorbed: a wall duration that cannot be
    trusted becomes ``None`` with a note, and a single unparseable cost withholds the whole
    total (the same posture the mixed-currency path already takes) instead of silently
    summing the remainder into an understated figure. "Unavailable", never a wrong number.

    The third element says whether ``wall_duration_ms`` covers every step. A duration built
    from a subset is a lower bound, which is honest to publish on its own but cannot be
    subtracted: the unobserved work has unknown length, so a delta computed from it has
    unknown magnitude *and* unknown sign.
    """
    notes: list[str] = []
    duration_complete = False
    starts: list[datetime] = []
    ends: list[datetime] = []
    reversed_steps = 0
    for step in nt.steps:
        start = _iso(step.ts)
        end = _iso(step.evidence.end_ts) if step.evidence else None
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
        # A step that ends before it began is internally inconsistent, and the aggregate
        # envelope hides it: with steps [0s,2s] and [10s,1s] the envelope max(ends)-min(starts)
        # is a healthy-looking +2s, and the 10s start never appears in the answer at all. Check
        # each pair on its own, not just the outer bounds.
        if start is not None and end is not None and end < start:
            reversed_steps += 1
    wall: float | None = None
    if starts and ends:
        wall = round((max(ends) - min(starts)).total_seconds() * 1000, 6)
        if reversed_steps:
            wall = None
            notes.append(
                f"wall_duration_ms unavailable: {reversed_steps} step(s) report an end before "
                "their own start; span timestamps are inconsistent."
            )
        elif wall < 0:
            # An end that precedes every start means the timestamps disagree (clock skew,
            # a mis-scaled unit, a mislabeled span). A negative duration is not a fact.
            wall = None
            notes.append(
                "wall_duration_ms unavailable: the latest observed end precedes the earliest "
                "observed start; span timestamps are inconsistent."
            )
        else:
            # Both ends of the span matter. A missing *start* is as distorting as a missing
            # end: the earliest step may be the one without a usable timestamp, in which case
            # min(starts) is later than the real beginning and the duration comes out short.
            total = len(nt.steps)
            gaps = []
            if len(starts) < total:
                gaps.append(f"{len(starts)} of {total} reported a usable start")
            if len(ends) < total:
                gaps.append(f"{len(ends)} of {total} reported an end")
            if gaps:
                notes.append(
                    "wall_duration_ms is a lower bound: " + "; ".join(gaps) + "."
                )
            else:
                duration_complete = True

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
    unusable_costs = 0
    for evidence in evidence_items:
        if evidence.total_cost is not None:
            try:
                parsed = Decimal(evidence.total_cost)
            except InvalidOperation:
                unusable_costs += 1
                continue
            if not (parsed.is_finite() and parsed >= 0):
                unusable_costs += 1
                continue
            costs.append(parsed)
            if evidence.cost_currency:
                cost_currencies.add(evidence.cost_currency)
            else:
                has_currencyless_cost = True
    ambiguous_cost = len(cost_currencies) > 1 or (
        bool(cost_currencies) and has_currencyless_cost
    )
    if unusable_costs:
        # Dropping the bad value and summing the rest would report a confident number that
        # is knowably too low. Withhold the total and say why.
        ambiguous_cost = True
        notes.append(
            f"total_cost unavailable: {unusable_costs} step(s) reported a cost that is not a "
            "finite non-negative decimal, so the remaining costs would understate the total."
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
    ), notes, duration_complete


def _logical_keys(nt: NormalizedTrace, table: dict | None = None) -> dict[int, Step]:
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

    if table is None:
        table = {}
    result: dict[int, Step] = {}
    queue = deque()
    for root in children.get(None, []):
        queue.append((root, None))
    while queue:
        step_id, prefix = queue.popleft()
        step = steps[step_id]
        signature = (prefix, step.kind.value, step.name, occurrence_of[step_id])
        key = table.setdefault(signature, len(table))
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
    current_duration_complete: bool,
    notes: list[str] | None = None,
) -> ComparisonSummary:
    """Compare against a baseline. Appends the baseline's own metric disclosures to ``notes``.

    A delta is only as trustworthy as both sides of it, so a baseline whose duration is a
    lower bound (or whose cost had to be withheld) must say so too — otherwise the report
    presents a confident metric_delta computed from a number it would not have published
    on its own.
    """
    structural = tree_diff(baseline, current, display_label=_display_name)
    table = {}
    before, after = _logical_keys(baseline, table), _logical_keys(current, table)
    signatures = {number: signature for signature, number in table.items()}

    def display_key(key):
        segments = []
        while key is not None:
            key, kind, name, occurrence = signatures[key]
            safe = _safe(name)
            tagged = ["redacted" if safe != name else "literal", safe]
            segments.append([kind, tagged, occurrence])
        return json.dumps(list(reversed(segments)), ensure_ascii=True, separators=(",", ":"))
    changes: list[BehaviorChange] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        old_status = old.status.value if old else None
        new_status = new.status.value if new else None
        if old_status != new_status:
            changes.append(
                BehaviorChange(
                    logical_step_key=display_key(key),
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
    bm, baseline_notes, baseline_duration_complete = _metrics_with_notes(baseline)
    cm = current_metrics
    cost_delta: str | None = None
    if (
        bm.total_cost is not None
        and cm.total_cost is not None
        and bm.cost_currency is not None
        and bm.cost_currency == cm.cost_currency
    ):
        cost_delta = format(Decimal(cm.total_cost) - Decimal(bm.total_cost), "f")
    if notes is not None:
        notes.extend(f"baseline: {note}" for note in baseline_notes)
    # A delta between two lower bounds is not a lower bound on the delta. If either side
    # missed a step, the unobserved work could be longer than the difference, so the number
    # would be wrong in magnitude and possibly in sign. Withhold it and say why; disclosing
    # the inputs' incompleteness does not license publishing a confident subtraction of them.
    durations_comparable = current_duration_complete and baseline_duration_complete
    if not durations_comparable and None not in (cm.wall_duration_ms, bm.wall_duration_ms):
        if notes is not None:
            notes.append(
                "wall_duration_ms delta unavailable: at least one side's duration covers "
                "only part of its trace, so the difference has unknown size and sign."
            )
    return ComparisonSummary(
        baseline_digest=_digest(baseline),
        topology_identical=structural.identical,
        topology_changes=structural.changes,
        behavior_changes=changes,
        pattern_count_deltas=deltas,
        metric_deltas={
            "wall_duration_ms": (
                _delta(cm.wall_duration_ms, bm.wall_duration_ms)
                if durations_comparable
                else None
            ),
            "prompt_tokens": _delta(cm.prompt_tokens, bm.prompt_tokens),
            "completion_tokens": _delta(cm.completion_tokens, bm.completion_tokens),
            "total_tokens": _delta(cm.total_tokens, bm.total_tokens),
            "total_cost": cost_delta,
        },
    )


def _digest(nt: NormalizedTrace) -> str:
    return "sha256:" + hashlib.sha256(artifact.dumps(nt).encode("utf-8")).hexdigest()


def analyze(nt: NormalizedTrace, *, baseline: NormalizedTrace | None = None) -> AnalysisReport:
    # Analyses index by step id and walk causes in seq order; on a malformed trace that
    # surfaces as a raw KeyError deep inside a helper. Reject it here as a ValueError so the
    # library entry point has the same failure contract as artifact.loads().
    validate_structure(nt)
    if baseline is not None:
        validate_structure(baseline)
    primary, propagated = _failures(nt)
    patterns = _patterns(nt)
    metrics, metric_notes, duration_complete = _metrics_with_notes(nt)
    warnings: list[str] = []
    if nt.trace.causal_fidelity is CausalFidelity.PARENT_ONLY:
        warnings.append(
            "Phoenix export preserves parent relationships only; additional fan-in causes may be missing."
        )
    for label, trace in (("current", nt), ("baseline", baseline)):
        if trace is None:
            continue
        if label == "baseline" and trace.trace.causal_fidelity is CausalFidelity.PARENT_ONLY:
            warnings.append("baseline: Phoenix export preserves parent relationships only; additional fan-in causes may be missing.")
        lossy = sum(step.projection_lossy for step in trace.steps)
        if lossy:
            warnings.append(f"{label}: derived tree drops causes at {lossy} step(s); topology comparison covers TREE_PARENT only.")
        if trace.trace.source_kind == "langgraph":
            # Unconditional on purpose. Capture now covers both the configured error channel
            # and native task failures, but an artifact ingested by an older build, or one
            # whose checkpoints use a layout this adapter does not recognize, carries no
            # derived failure step and looks identical to a clean run. Absence of a failure
            # is therefore still not evidence of success, and nothing in the artifact says
            # which case applies — so the disclosure cannot be made conditional.
            warnings.append(f"{label}: LangGraph failure capture covers the configured error channel and task pending writes for recognized checkpoint layouts; absence of an observed failure does not prove execution success.")
        if trace.trace.links_preserved:
            warnings.append(f"{label}: link preservation covers valid in-trace links only; foreign or unresolved links are omitted.")
    if primary or propagated:
        warnings.append("Failure candidates are investigation leads. Containment/unknown edges do not prove propagation; explicit causal ancestry is context, not proof of exception propagation.")
    if any(getattr(metrics, field) is not None for field in ("prompt_tokens", "completion_tokens", "total_tokens", "total_cost")):
        warnings.append("Metrics sum observed span values; coverage may be partial and producer aggregates may double count.")
    # Withheld or partially-covered metrics are disclosed, never silently absorbed. The
    # comparison is built first because it contributes the baseline's own disclosures: a
    # delta is only as trustworthy as both sides of it.
    comparison = (
        _comparison(
            nt,
            baseline,
            current_patterns=patterns,
            current_metrics=metrics,
            current_duration_complete=duration_complete,
            notes=metric_notes,
        )
        if baseline
        else None
    )
    warnings.extend(metric_notes)
    report = AnalysisReport(
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
        comparison=comparison,
        warnings=warnings,
    )
    def clean(value):
        return _safe(value)

    report.source_kind = clean(report.source_kind)
    report.metrics.cost_currency = clean(report.metrics.cost_currency)
    for finding in report.primary_failures:
        for step in [finding.step, *finding.causal_steps]:
            step.name = clean(step.name)
    for step in report.propagated_failures:
        step.name = clean(step.name)
    for finding in report.patterns:
        finding.labels = [clean(name) for name in finding.labels]
    for item in report.metrics.evaluations:
        item.name, item.label = clean(item.name), clean(item.label)
    if report.comparison:
        for change in report.comparison.behavior_changes:
            change.name = clean(change.name)
    # Disclose redaction if and only if an alias actually survives into the report. Deriving
    # the flag from the finished document (rather than from every clean() call) covers text
    # that only reaches the report indirectly — baseline topology descriptions and logical
    # step keys — without raising the warning on names that were never published at all. A
    # disclosure attached to a report containing zero aliases just teaches readers to skip it.
    if _ALIAS.search(dumps(report)):
        report.warnings.append("Display text was replaced with deterministic redacted aliases; safe-v1 retains structural identifiers and is not anonymization.")
    return report


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
