"""Group a preset's matches across an artifact cohort by a versioned, body-free signature.

A group is an observed preset shape, nothing more: two matches share a group when their steps
agree on kind, status, source and safe-v1 name, and their edges on origin, under the same
source and causal fidelity. Equal shapes do not show equal raw causality or a shared root
cause. Only confirmed matches are grouped (``check``'s rule: every field a predicate constrains
is known on the step it binds), and a field that is not known is recorded as unknown rather
than by the value that stands in for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from tracegraph._atomic_write import write_text_atomic
from tracegraph.analysis.check import _HEURISTIC_EDGES, _constrained, _could_be_hidden, _known
from tracegraph.analysis.diagnose import _safe
from tracegraph.analysis.patterns import PathPattern, build_index, iter_matches
from tracegraph.model import CAPTURE_GAP_CODES, EdgeType, NormalizedTrace, Step

MOTIF_SUMMARY_SCHEMA_VERSION: Final = 1
MOTIF_SUMMARY_KIND: Final = "tracegraph.motif-summary"
SIGNATURE_VERSION: Final = "motif-signature/v1"
MAX_REPRESENTATIVES: Final = 3

_SIGNED_FIELDS = ("kind", "name", "status")
HeuristicEdges = Literal["disclosed", "not_disclosed", "unknown", "not_applicable"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StepShape(_Strict):
    kind: str | None
    name: str | None
    status: str | None
    source: str
    #: The signed fields whose value is not known; each is ``null`` above.
    unknown: list[str]


class MotifSignature(_Strict):
    pattern_id: str
    pattern_version: int
    source_kind: str
    causal_fidelity: str
    links_preserved: bool | None
    #: Whether the trace discloses causal edges chosen heuristically at ingest (an OTLP parent
    #: picked by timing, or a fallback). The disclosure is per trace, not per edge, so it does not
    #: say which edge. ``unknown`` when the trace has no capture record or a gap code this build
    #: does not know; ``not_applicable`` for a single-step preset, whose shape has no edge.
    heuristic_edges: HeuristicEdges
    steps: list[StepShape]
    #: The CAUSED_BY origin between consecutive matched steps (``null`` when unrecorded).
    edge_origins: list[str | None]


class Representative(_Strict):
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    step_ids: list[str]


class MotifGroup(_Strict):
    signature: MotifSignature
    match_count: int
    trace_count: int
    representatives: list[Representative]


class Coverage(_Strict):
    artifacts_discovered: int
    artifacts_valid: int
    artifacts_skipped: int
    traces_matched: int
    #: Traces where resolving something unknown could add a match (``check``'s per-preset rule).
    traces_may_hide_matches: int
    traces_with_disclosed_gaps: int
    #: Traces with no capture record: what they did not capture is unknown.
    traces_without_provenance: int
    unconfirmed_matches: int
    traces_with_unconfirmed_matches: int


class PatternRef(_Strict):
    id: str
    version: int


class MotifSummary(_Strict):
    schema_version: Literal[1] = MOTIF_SUMMARY_SCHEMA_VERSION
    kind: Literal["tracegraph.motif-summary"] = MOTIF_SUMMARY_KIND
    signature_version: Literal["motif-signature/v1"] = SIGNATURE_VERSION
    privacy_profile: Literal["safe-v1"] = "safe-v1"
    pattern: PatternRef
    coverage: Coverage
    groups: list[MotifGroup]


def is_summarizable(pattern: PathPattern) -> bool:
    """Whether every predicate after the first is strictly adjacent to the one before.

    Between gap endpoints the matched path is not recorded, so its edge evidence cannot be part
    of a v1 signature.
    """
    return all(p.gap in (None, (1, 1)) for p in pattern.steps[1:])


def _heuristic_edges(nt: NormalizedTrace) -> HeuristicEdges:
    """A disclosure, not ``check``'s may-hide rule: an unrecognized code is unknown, not "yes"."""
    if nt.trace.capture is None:
        return "unknown"
    codes = {gap.code for gap in nt.trace.capture.gaps}
    if codes & _HEURISTIC_EDGES:
        return "disclosed"
    return "unknown" if codes - CAPTURE_GAP_CODES else "not_disclosed"


def _shape(step: Step) -> StepShape:
    unknown = [field for field in _SIGNED_FIELDS if not _known(step, field)]
    return StepShape(
        kind=None if "kind" in unknown else step.kind.value,
        name=None if "name" in unknown else _safe(step.name),
        status=None if "status" in unknown else step.status.value,
        source=step.source.value,
        unknown=unknown,
    )


class _Group:
    __slots__ = ("signature", "match_count", "trace_count", "representatives")

    def __init__(self, signature: MotifSignature) -> None:
        self.signature = signature
        self.match_count = 0
        self.trace_count = 0
        self.representatives: list[tuple[str, list[str]]] = []

    def offer(self, digest: str, step_ids: list[str]) -> None:
        candidate = (digest, step_ids)
        reps = self.representatives
        if len(reps) == MAX_REPRESENTATIVES and candidate >= reps[-1]:
            return
        reps.append(candidate)
        reps.sort()
        del reps[MAX_REPRESENTATIVES:]


class MotifAggregator:
    """Fold one trace at a time into per-signature counts; matches are never collected."""

    def __init__(self, pattern: PathPattern) -> None:
        if pattern.pattern_id is None or pattern.pattern_version is None:
            raise ValueError("a motif summary requires a versioned preset pattern")
        if not is_summarizable(pattern):
            raise ValueError(
                f"preset {pattern.pattern_id!r} is not summarizable in {SIGNATURE_VERSION}: the "
                "matched path between gap endpoints is not recorded, so its edge evidence "
                "cannot be grouped"
            )
        self._pattern = pattern
        self._constrained = _constrained(pattern)
        self._groups: dict[str, _Group] = {}
        self._traces_matched = 0
        self._may_hide = 0
        self._disclosed = 0
        self._without_provenance = 0
        self._unconfirmed = 0
        self._traces_unconfirmed = 0

    def add(self, nt: NormalizedTrace, digest: str) -> None:
        pattern = self._pattern
        capture = nt.trace.capture
        if capture is None:
            self._without_provenance += 1
        elif capture.gaps:
            self._disclosed += 1
        if _could_be_hidden(nt, pattern):
            self._may_hide += 1

        index = build_index(nt)
        steps = index[0]
        origins = {
            (e.src, e.dst): (e.origin.value if e.origin is not None else None)
            for e in nt.edges_of(EdgeType.CAUSED_BY)
        }
        heuristic: HeuristicEdges = (
            _heuristic_edges(nt) if len(pattern.steps) > 1 else "not_applicable"
        )
        header = (_safe(nt.trace.source_kind), nt.trace.causal_fidelity.value,
                  nt.trace.links_preserved, heuristic)
        hit: set[str] = set()
        unconfirmed = 0
        for path in iter_matches(nt, pattern, index=index):
            bound = [steps[sid] for sid in path]
            if not all(
                _known(step, field)
                for fields, step in zip(self._constrained, bound, strict=True)
                for field in fields
            ):
                unconfirmed += 1
                continue
            signature = MotifSignature(
                pattern_id=pattern.pattern_id,  # type: ignore[arg-type]
                pattern_version=pattern.pattern_version,  # type: ignore[arg-type]
                source_kind=header[0],
                causal_fidelity=header[1],
                links_preserved=header[2],
                heuristic_edges=header[3],
                steps=[_shape(step) for step in bound],
                # Pattern order runs cause -> effect; a CAUSED_BY edge points effect -> cause.
                edge_origins=[origins[(effect, cause)] for cause, effect in zip(path, path[1:], strict=False)],
            )
            key = json.dumps(signature.model_dump(mode="json"), sort_keys=True)
            group = self._groups.get(key)
            if group is None:
                group = self._groups[key] = _Group(signature)
            group.match_count += 1
            group.offer(digest, path)
            hit.add(key)
        for key in hit:
            self._groups[key].trace_count += 1
        if hit:
            self._traces_matched += 1
        if unconfirmed:
            self._unconfirmed += unconfirmed
            self._traces_unconfirmed += 1

    def summary(self, *, discovered: int, valid: int, skipped: int) -> MotifSummary:
        ordered = sorted(
            self._groups.items(), key=lambda kv: (-kv[1].trace_count, -kv[1].match_count, kv[0])
        )
        return MotifSummary(
            pattern=PatternRef(id=self._pattern.pattern_id,  # type: ignore[arg-type]
                               version=self._pattern.pattern_version),  # type: ignore[arg-type]
            coverage=Coverage(
                artifacts_discovered=discovered,
                artifacts_valid=valid,
                artifacts_skipped=skipped,
                traces_matched=self._traces_matched,
                traces_may_hide_matches=self._may_hide,
                traces_with_disclosed_gaps=self._disclosed,
                traces_without_provenance=self._without_provenance,
                unconfirmed_matches=self._unconfirmed,
                traces_with_unconfirmed_matches=self._traces_unconfirmed,
            ),
            groups=[
                MotifGroup(
                    signature=group.signature,
                    match_count=group.match_count,
                    trace_count=group.trace_count,
                    representatives=[
                        Representative(artifact_digest=digest, step_ids=list(step_ids))
                        for digest, step_ids in group.representatives
                    ],
                )
                for _, group in ordered
            ],
        )


def dumps(summary: MotifSummary) -> str:
    return json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def save_atomic(summary: MotifSummary, path: Path) -> None:
    write_text_atomic(path, dumps(summary), newline="\n")
