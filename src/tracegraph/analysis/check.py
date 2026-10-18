"""Explicit regression check: PASS, FAIL or INSUFFICIENT_EVIDENCE for a current/baseline pair.

The check judges recorded evidence only. It never runs an agent, never picks a baseline and
never enforces a policy; the caller names both traces and every rule. Each rule decides on its
own, and an input that cannot support a decision makes that rule INSUFFICIENT_EVIDENCE rather
than PASS or FAIL.

Two facts shape every rule:

* **No source records which step of one run is which step of another.** Positional keys shift
  when a sibling is deleted or a wrapper inserted, so rules compare counts per step signature
  ``(kind, name)``. Two same-signature steps exchanging which one fails is not detected;
  ``analyze --baseline`` keeps the positional view.
* **Unknown is an interval, never a zero.** A step's ``unset`` status, a value the adapter
  substituted (``Step.unknown_fields``) and a capture gap that can hide a failure all leave the
  true count open. Every preset predicate is positive, so with substituted values excluded the
  observed counts are lower bounds: FAIL needs a baseline with nothing unknown that could
  matter, PASS needs such a current.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracegraph._atomic_write import write_text_atomic
from tracegraph.analysis.ahu import default_label, diff as tree_diff, structure_only
from tracegraph.analysis.diagnose import _ALIAS, _digest, _display_name, _safe
from tracegraph.analysis.patterns import PRESETS, PathPattern, StepPredicate, build_index, find_matches
from tracegraph.model import (
    CAPTURE_GAP_CODES,
    NormalizedTrace,
    Step,
    StepStatus,
)
from tracegraph.normalize import ProjectionRule, validate_normalized

CHECK_SCHEMA_VERSION: Final = 1
CHECK_KIND: Final = "tracegraph.check"

Outcome = Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]

#: What each capture gap can hide. A code this build does not know is assumed to hide all of it.
_HIDES_FAILURES = frozenset({"langgraph.malformed_pending_writes", "langgraph.undecided_interrupt"})
_HIDES_SCHEDULED = frozenset({"langgraph.unapplied_command_goto", "langgraph.unknown_send_target"})
_HEURISTIC_EDGES = frozenset({"otlp.graph_parent_by_timing", "otlp.graph_parent_fallback"})

LANGGRAPH_LIMITATION = (
    "LangGraph checkpoint layouts this adapter does not recognize record no failure, so a "
    "failure there would be absent from both counts."
)
REDACTION_WARNING = (
    "Display text was replaced with deterministic redacted aliases; safe-v1 retains structural "
    "identifiers and is not anonymization."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


_DIGEST = r"^sha256:[0-9a-f]{64}$"


class CaptureGapSummary(_Strict):
    code: str
    count: int = Field(ge=1)


class TraceContext(_Strict):
    artifact_digest: str = Field(pattern=_DIGEST)
    trace_id: str
    source_kind: str
    causal_fidelity: Literal["declared_dag", "parent_only", "legacy_unknown"]
    links_preserved: bool | None
    projection_rule: Literal["origin_priority", "temporal"]
    schedule_graph_fingerprint: str | None = Field(pattern=_DIGEST)
    step_count: int = Field(ge=0)
    #: ``None`` when the artifact predates capture provenance.
    capture_gaps: list[CaptureGapSummary] | None


class StepSignature(_Strict):
    kind: str
    name: str | None


class FailureEvidence(_Strict):
    #: ``None`` for failures whose kind or name the adapter could not establish.
    signature: StepSignature | None
    baseline_errors: int = Field(ge=0)
    current_errors: int = Field(ge=0)
    current_step_ids: list[str]


class PatternEvidence(_Strict):
    #: ``None`` for matches that bind a step whose kind or name is unknown.
    signature: list[StepSignature] | None
    baseline_matches: int = Field(ge=0)
    current_matches: int = Field(ge=0)
    current_step_ids: list[list[str]]


class TopologyEvidence(_Strict):
    change: str


RuleId = Literal["failures-not-increased", "pattern-not-increased", "topology-unchanged"]

#: Each rule's parameter names and evidence type, enforced by the model and by the schema.
_RULE_SHAPES: dict[str, tuple[frozenset[str], type]] = {
    "failures-not-increased": (frozenset(), FailureEvidence),
    "pattern-not-increased": (frozenset({"preset", "pattern_version"}), PatternEvidence),
    "topology-unchanged": (frozenset({"compare"}), TopologyEvidence),
}


class RuleResult(_Strict):
    rule_id: RuleId
    rule_version: Literal[1] = 1
    params: dict[str, str | int]
    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence: list[FailureEvidence | PatternEvidence | TopologyEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape_matches_rule(self) -> "RuleResult":
        names, evidence_type = _RULE_SHAPES[self.rule_id]
        if set(self.params) != names:
            raise ValueError(f"{self.rule_id} takes parameters {sorted(names)}")
        if self.rule_id == "pattern-not-increased" and not (
            isinstance(self.params["preset"], str)
            and isinstance(self.params["pattern_version"], int)
            and self.params["pattern_version"] >= 1
        ):
            raise ValueError("pattern-not-increased needs a preset name and a positive pattern_version")
        if self.rule_id == "topology-unchanged" and self.params["compare"] not in ("labels", "shape"):
            raise ValueError("topology-unchanged compares 'labels' or 'shape'")
        if any(not isinstance(item, evidence_type) for item in self.evidence):
            raise ValueError(f"{self.rule_id} evidence must be {evidence_type.__name__}")
        return self


class CheckReport(_Strict):
    schema_version: Literal[1] = CHECK_SCHEMA_VERSION
    kind: Literal["tracegraph.check"] = CHECK_KIND
    verdict: Outcome
    current: TraceContext
    baseline: TraceContext
    rules: list[RuleResult] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    privacy_profile: Literal["safe-v1"] = "safe-v1"


# --- rule selection --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSpec:
    rule_id: RuleId
    params: tuple[tuple[str, str | int], ...]

    def key(self) -> tuple[str, str]:
        return self.rule_id, json.dumps(dict(self.params), sort_keys=True)


RULE_HELP = (
    "failures-not-increased, pattern-not-increased:<preset>, "
    "topology-unchanged, topology-unchanged:shape"
)


def parse_rules(texts: list[str]) -> list[RuleSpec]:
    """Parse ``--rule`` values, de-duplicated and in canonical order. Raises ``ValueError``."""
    if not texts:
        raise ValueError(f"at least one --rule is required ({RULE_HELP})")
    specs: dict[tuple[str, str], RuleSpec] = {}
    for text in texts:
        rule_id, _, arg = text.partition(":")
        if rule_id == "failures-not-increased" and not arg:
            spec = RuleSpec("failures-not-increased", ())
        elif rule_id == "pattern-not-increased" and arg:
            pattern = PRESETS.get(arg)
            if pattern is None:
                raise ValueError(f"unknown preset {arg!r}; available: {', '.join(PRESETS)}")
            spec = RuleSpec(
                "pattern-not-increased", (("pattern_version", pattern.pattern_version or 1), ("preset", arg))
            )
        elif rule_id == "topology-unchanged" and arg in ("", "shape"):
            spec = RuleSpec("topology-unchanged", (("compare", "shape" if arg else "labels"),))
        else:
            raise ValueError(f"unknown rule {text!r}; available: {RULE_HELP}")
        specs.setdefault(spec.key(), spec)
    return [specs[key] for key in sorted(specs)]


# --- what each side knows --------------------------------------------------------------------


def _gap_codes(nt: NormalizedTrace) -> set[str]:
    return {gap.code for gap in nt.trace.capture.gaps} if nt.trace.capture else set()


def _gap_hides(nt: NormalizedTrace, effect: frozenset[str]) -> bool:
    codes = _gap_codes(nt)
    return bool(codes & effect) or bool(codes - CAPTURE_GAP_CODES)


def _signature_known(step: Step) -> bool:
    return _known(step, "kind") and _known(step, "name")


def _signature(step: Step) -> tuple[str, str | None]:
    return step.kind.value, step.name


def _shown(signature: tuple[str, str | None]) -> StepSignature:
    return StepSignature(kind=signature[0], name=_safe(signature[1]))


def _known(step: Step, field: str) -> bool:
    if field == "status":
        return step.status is not StepStatus.UNSET
    if field == "error_msg" and step.status is StepStatus.UNSET:
        # A step with no recorded outcome has no message either; should it have failed, the
        # message is as unknown as the failure.
        return False
    if field == "name" and step.name is None:
        # A missing name cannot be told apart from an unrecorded one here, and a comparison
        # that needs the name must not read either as "different".
        return False
    return field not in step.unknown_fields


def _constrained(pattern: PathPattern) -> list[set[str]]:
    """The fields each predicate constrains. A back-reference constrains the name of *both*
    steps it relates: the referenced predicate may itself say nothing about names."""
    fields: list[set[str]] = []
    for pred in pattern.steps:
        own = set()
        if pred.kind is not None:
            own.add("kind")
        if pred.name is not None or pred.name_prefix is not None or pred.same_name_as is not None:
            own.add("name")
        if pred.status is not None:
            own.add("status")
        if pred.error_contains is not None:
            own.add("error_msg")
        fields.append(own)
    for pred in pattern.steps:
        if pred.same_name_as is not None:
            fields[pred.same_name_as].add("name")
    return fields


def _satisfies_known(pred: StepPredicate, step: Step) -> bool:
    """Whether the step's *known* fields satisfy the predicate; unknown ones are assumed to."""
    if _known(step, "name"):
        if pred.name is not None and step.name != pred.name:
            return False
        if pred.name_prefix is not None and not (step.name or "").startswith(pred.name_prefix):
            return False
    if _known(step, "error_msg") and pred.error_contains is not None and pred.error_contains not in (step.error_msg or ""):
        return False
    if _known(step, "kind") and pred.kind is not None and step.kind is not pred.kind:
        return False
    if _known(step, "status") and pred.status is not None and step.status is not pred.status:
        return False
    return True


def _could_be_hidden(nt: NormalizedTrace, pattern: PathPattern) -> bool:
    """Whether resolving something unknown on this side could add a match.

    A step is relevant only when a predicate constrains a field that is unknown on it and the
    step's known fields satisfy the rest; an unset CHAIN cannot become a failing TOOL, and a step
    between gap endpoints is constrained by nothing. A gap that can hide a failure counts for any
    preset that constrains status.
    """
    if any(pred.status is not None for pred in pattern.steps) and _gap_hides(nt, _HIDES_FAILURES):
        return True
    constrained = _constrained(pattern)
    for step in nt.steps:
        for pred, fields in zip(pattern.steps, constrained, strict=True):
            unknown = {field for field in fields if not _known(step, field)}
            if unknown and _satisfies_known(pred, step):
                return True
    return False


# --- context and preconditions ---------------------------------------------------------------


def _context(nt: NormalizedTrace) -> TraceContext:
    rule = validate_normalized(nt)
    return TraceContext(
        artifact_digest=_digest(nt),
        trace_id=nt.trace.trace_id,
        source_kind=_safe(nt.trace.source_kind),
        causal_fidelity=nt.trace.causal_fidelity.value,
        links_preserved=nt.trace.links_preserved,
        projection_rule="temporal" if rule is ProjectionRule.TEMPORAL else "origin_priority",
        schedule_graph_fingerprint=nt.trace.schedule_graph.fingerprint if nt.trace.schedule_graph else None,
        step_count=len(nt.steps),
        capture_gaps=(
            [CaptureGapSummary(code=gap.code, count=gap.count) for gap in nt.trace.capture.gaps]
            if nt.trace.capture is not None
            else None
        ),
    )


def _common_reasons(current: NormalizedTrace, baseline: NormalizedTrace) -> list[str]:
    reasons = []
    for label, nt in (("current", current), ("baseline", baseline)):
        if nt.trace.capture is None:
            reasons.append(
                f"{label}: capture provenance unavailable (artifact written before schema "
                "version 7); re-ingest with this release"
            )
        if not nt.steps:
            reasons.append(f"{label}: the trace has no steps")
    if current.trace.source_kind != baseline.trace.source_kind:
        reasons.append(
            "the traces come from different sources, whose step granularity and status "
            "semantics differ"
        )
    return reasons


def _causal_reasons(current: NormalizedTrace, baseline: NormalizedTrace) -> list[str]:
    reasons = []
    if (current.trace.causal_fidelity, current.trace.links_preserved) != (
        baseline.trace.causal_fidelity, baseline.trace.links_preserved
    ):
        reasons.append("the traces were read with different causal settings (fidelity or links)")
    for label, nt in (("current", current), ("baseline", baseline)):
        if _gap_hides(nt, _HEURISTIC_EDGES):
            reasons.append(f"{label}: some causal edges were chosen heuristically at ingest")
    return reasons


# --- rules -----------------------------------------------------------------------------------


def _decide(increased: bool, baseline_unknown: bool, current_unknown: bool) -> Outcome:
    if increased and not baseline_unknown:
        return "FAIL"
    if not increased and not current_unknown:
        return "PASS"
    return "INSUFFICIENT_EVIDENCE"


def _failure_counts(nt: NormalizedTrace) -> tuple[Counter, dict, int, bool]:
    """Errors per known signature, their step ids, errors of unknown signature, and whether
    anything on this side could still be a failure nobody counted."""
    by_signature: Counter = Counter()
    ids: dict[tuple[str, str | None] | None, list[str]] = {}
    unknown_signature = 0
    for step in nt.steps:
        if step.status is not StepStatus.ERROR:
            continue
        key = _signature(step) if _signature_known(step) else None
        if key is None:
            unknown_signature += 1
        else:
            by_signature[key] += 1
        ids.setdefault(key, []).append(step.step_id)
    open_ended = (
        unknown_signature > 0
        or any(step.status is StepStatus.UNSET for step in nt.steps)
        or _gap_hides(nt, _HIDES_FAILURES)
    )
    return by_signature, ids, unknown_signature, open_ended


def _failures_rule(current: NormalizedTrace, baseline: NormalizedTrace) -> tuple[Outcome, list[str], list[FailureEvidence]]:
    cur, cur_ids, cur_unknown, cur_open = _failure_counts(current)
    base, _, base_unknown, base_open = _failure_counts(baseline)
    increased_signatures = sorted(
        (key for key in cur if cur[key] > base[key]), key=lambda key: (key[0], key[1] or "")
    )
    total_increased = sum(cur.values()) + cur_unknown > sum(base.values()) + base_unknown
    outcome = _decide(bool(increased_signatures) or total_increased, base_open, cur_open)
    reasons = []
    if outcome == "INSUFFICIENT_EVIDENCE":
        if base_open and (increased_signatures or total_increased):
            reasons.append("baseline: an unset step, a failure of unknown kind or name, or a "
                           "capture gap could account for the difference")
        if cur_open:
            reasons.append("current: an unset step, a failure of unknown kind or name, or a "
                           "capture gap could hide a failure")
    evidence = [
        FailureEvidence(
            signature=_shown(key),
            baseline_errors=base[key],
            current_errors=cur[key],
            current_step_ids=sorted(cur_ids[key]),
        )
        for key in increased_signatures
    ]
    if cur_unknown:
        evidence.append(
            FailureEvidence(
                signature=None,
                baseline_errors=base_unknown,
                current_errors=cur_unknown,
                current_step_ids=sorted(cur_ids[None]),
            )
        )
    return outcome, reasons, evidence


def _pattern_matches(nt: NormalizedTrace, pattern: PathPattern) -> tuple[Counter, dict, int]:
    """Confirmed matches per signature, their step ids, and confirmed matches whose signature
    is unknown. A match is confirmed when no field a predicate constrains is unknown on the step
    it binds: a substituted value (a defaulted CHAIN) can satisfy a predicate it should not."""
    index = build_index(nt)
    steps = index[0]
    constrained = _constrained(pattern)
    by_signature: Counter = Counter()
    ids: dict[tuple | None, list[list[str]]] = {}
    unknown_signature = 0
    for match in find_matches(nt, pattern, index=index):
        bound = [steps[sid] for sid in match]
        if not all(
            _known(step, field)
            for fields, step in zip(constrained, bound, strict=True)
            for field in fields
        ):
            continue
        key = tuple(_signature(step) for step in bound) if all(map(_signature_known, bound)) else None
        if key is None:
            unknown_signature += 1
        else:
            by_signature[key] += 1
        ids.setdefault(key, []).append(list(match))
    return by_signature, ids, unknown_signature


def _pattern_rule(
    current: NormalizedTrace, baseline: NormalizedTrace, pattern: PathPattern
) -> tuple[Outcome, list[str], list[PatternEvidence]]:
    cur, cur_ids, cur_unknown = _pattern_matches(current, pattern)
    base, _, base_unknown = _pattern_matches(baseline, pattern)
    cur_open = cur_unknown > 0 or _could_be_hidden(current, pattern)
    base_open = base_unknown > 0 or _could_be_hidden(baseline, pattern)
    increased = sorted((key for key in cur if cur[key] > base[key]), key=lambda key: json.dumps(key))
    total_increased = sum(cur.values()) + cur_unknown > sum(base.values()) + base_unknown
    outcome = _decide(bool(increased) or total_increased, base_open, cur_open)
    reasons = []
    if outcome == "INSUFFICIENT_EVIDENCE":
        if base_open and (increased or total_increased):
            reasons.append("baseline: a step this preset could match has an unknown status, "
                           "kind, name or message, or a capture gap could hide a match")
        if cur_open:
            reasons.append("current: a step this preset could match has an unknown status, "
                           "kind, name or message, or a capture gap could hide a match")
    evidence = [
        PatternEvidence(
            signature=[_shown(part) for part in key],
            baseline_matches=base[key],
            current_matches=cur[key],
            current_step_ids=sorted(cur_ids[key]),
        )
        for key in increased
    ]
    if cur_unknown:
        evidence.append(
            PatternEvidence(
                signature=None,
                baseline_matches=base_unknown,
                current_matches=cur_unknown,
                current_step_ids=sorted(cur_ids[None]),
            )
        )
    return outcome, reasons, evidence


def _topology_rule(
    current: NormalizedTrace, baseline: NormalizedTrace, compare: str, same_projection: bool
) -> tuple[Outcome, list[str], list[TopologyEvidence]]:
    reasons = _causal_reasons(current, baseline)
    if not same_projection:
        reasons.append("the traces' derived trees were projected under different rules")
    if (current.trace.schedule_graph, baseline.trace.schedule_graph) != (None, None):
        if current.trace.schedule_graph != baseline.trace.schedule_graph:
            reasons.append("the traces were read against different compiled graphs")
        for label, nt in (("current", current), ("baseline", baseline)):
            if _gap_hides(nt, _HIDES_SCHEDULED):
                reasons.append(f"{label}: some scheduled tasks could not be listed at ingest")
    if compare == "labels":
        for label, nt in (("current", current), ("baseline", baseline)):
            if any(not _signature_known(step) for step in nt.steps):
                reasons.append(f"{label}: some step labels (kind or name) are unknown; "
                               "use topology-unchanged:shape to compare shape only")
    if reasons:
        return "INSUFFICIENT_EVIDENCE", reasons, []
    labels = default_label if compare == "labels" else structure_only
    result = tree_diff(baseline, current, labels, display_label=_display_name)
    if result.identical:
        return "PASS", [], []
    return "FAIL", [], [TopologyEvidence(change=change) for change in result.changes]


def evaluate(current: NormalizedTrace, baseline: NormalizedTrace, rules: list[RuleSpec]) -> CheckReport:
    """Evaluate ``rules`` over the pair. Raises ``ValueError`` for a malformed trace."""
    contexts = (_context(current), _context(baseline))
    common = _common_reasons(current, baseline)
    langgraph = "langgraph" in (current.trace.source_kind, baseline.trace.source_kind)
    results: list[RuleResult] = []
    for spec in rules:
        params = dict(spec.params)
        evidence: list = []
        limitations: list[str] = []
        outcome: Outcome
        if common:
            outcome, reasons = "INSUFFICIENT_EVIDENCE", list(common)
        elif spec.rule_id == "failures-not-increased":
            outcome, reasons, evidence = _failures_rule(current, baseline)
        elif spec.rule_id == "pattern-not-increased":
            pattern = PRESETS[str(params["preset"])]
            reasons = _causal_reasons(current, baseline) if len(pattern.steps) > 1 else []
            if reasons:
                outcome = "INSUFFICIENT_EVIDENCE"
            else:
                outcome, reasons, evidence = _pattern_rule(current, baseline, pattern)
        else:
            same_projection = contexts[0].projection_rule == contexts[1].projection_rule
            outcome, reasons, evidence = _topology_rule(
                current, baseline, str(params["compare"]), same_projection
            )
        if outcome == "PASS" and langgraph and spec.rule_id != "topology-unchanged":
            # What still stands behind a LangGraph PASS once no capture gap could hide a failure.
            limitations.append(LANGGRAPH_LIMITATION)
        results.append(
            RuleResult(
                rule_id=spec.rule_id,
                params=params,
                outcome=outcome,
                reasons=reasons,
                limitations=limitations,
                evidence=evidence,
            )
        )
    outcomes = {result.outcome for result in results}
    verdict: Outcome = (
        "FAIL" if "FAIL" in outcomes
        else "INSUFFICIENT_EVIDENCE" if "INSUFFICIENT_EVIDENCE" in outcomes
        else "PASS"
    )
    report = CheckReport(verdict=verdict, current=contexts[0], baseline=contexts[1], rules=results)
    if _ALIAS.search(dumps(report)):
        report.warnings.append(REDACTION_WARNING)
    return report


def dumps(report: CheckReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def save_atomic(report: CheckReport, path: str | Path) -> None:
    text = dumps(report)
    CheckReport.model_validate(report.model_dump(mode="json"))
    write_text_atomic(Path(path), text)
