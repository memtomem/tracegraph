"""Versioned, body-free review candidates produced from pattern matches."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tracegraph.analysis.patterns import Match, PathPattern
from tracegraph.model import NormalizedTrace, StepKind, StepStatus


REVIEW_CANDIDATE_SCHEMA_VERSION = 1
REVIEW_CANDIDATE_KIND = "tracegraph.review-candidates"


class ReviewCandidate(BaseModel):
    """One minimal governance-review input; no trace body or local path is retained."""

    model_config = ConfigDict(extra="allow")

    run_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    pattern_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    pattern_version: int = Field(ge=1)
    tool_key: str = Field(min_length=4, max_length=512, pattern=r"^\S+::\S+$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReviewCandidateReport(BaseModel):
    """Schema-v1 deterministic envelope for zero or more review candidates."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = REVIEW_CANDIDATE_SCHEMA_VERSION
    kind: Literal["tracegraph.review-candidates"] = REVIEW_CANDIDATE_KIND
    candidates: list[ReviewCandidate] = Field(default_factory=list)


def is_review_exportable(pattern: PathPattern) -> bool:
    """A review candidate must end at an explicitly failing TOOL predicate."""
    if not pattern.steps:
        return False
    endpoint = pattern.steps[-1]
    return endpoint.kind is StepKind.TOOL and endpoint.status is StepStatus.ERROR


def build_report(
    pattern: PathPattern,
    matches: list[Match],
    traces: dict[str, NormalizedTrace],
    artifact_digests: dict[str, str],
) -> ReviewCandidateReport:
    """Build a sorted, de-duplicated report from matches and their exact artifacts."""
    if pattern.pattern_id is None or pattern.pattern_version is None:
        raise ValueError("review export requires a versioned preset pattern")
    if not is_review_exportable(pattern):
        raise ValueError(f"preset {pattern.pattern_id!r} is not review-exportable")

    steps_by_trace = {trace_id: trace.steps_by_id() for trace_id, trace in traces.items()}
    unique: dict[tuple[str, str, int, str, str], ReviewCandidate] = {}
    for match in matches:
        trace = traces[match.trace_id]
        run_id = trace.trace.run_id
        if run_id is None:
            raise ValueError(f"trace {match.trace_id!r} has no run_id")
        endpoint = steps_by_trace[match.trace_id][match.step_ids[-1]]
        if endpoint.kind is not StepKind.TOOL or not endpoint.name:
            raise ValueError(f"trace {match.trace_id!r} matched an unnamed or non-TOOL endpoint")
        tool_key = endpoint.name
        if any(ch.isspace() for ch in tool_key) or "::" not in tool_key:
            raise ValueError(
                f"trace {match.trace_id!r} matched a TOOL without a server-qualified tool_key"
            )
        server, tool = tool_key.split("::", 1)
        if not server or not tool:
            raise ValueError(
                f"trace {match.trace_id!r} matched a TOOL without a server-qualified tool_key"
            )
        digest = artifact_digests[match.trace_id]
        candidate = ReviewCandidate(
            run_id=run_id,
            pattern_id=pattern.pattern_id,
            pattern_version=pattern.pattern_version,
            tool_key=tool_key,
            artifact_digest=digest,
        )
        key = (
            candidate.run_id,
            candidate.pattern_id,
            candidate.pattern_version,
            candidate.tool_key,
            candidate.artifact_digest,
        )
        unique[key] = candidate

    return ReviewCandidateReport(candidates=[unique[key] for key in sorted(unique)])


def dumps(report: ReviewCandidateReport) -> str:
    """Stable JSON representation used by the CLI and contract goldens."""
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def save_atomic(report: ReviewCandidateReport, path: str | Path) -> None:
    """Atomically replace ``path`` without leaving a partial report on failure."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
