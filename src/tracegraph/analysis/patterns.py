"""Cross-trace pattern matching over the **raw** causal graph.

The headline "graph queries, not columns" capability: find a causal *sequence* of steps
(e.g. "a tool step that errored", "plan immediately followed by a failing tool") across many
traces at once. Reads the raw ``CAUSED_BY`` layer — patterns are about real causality, so they
must see every real predecessor, not the lossy single-parent projection.

A ``PathPattern`` is a backend-neutral spec: an ordered list of ``StepPredicate``. The matcher
here is pure-Python (zero optional deps); the *same spec* is what a future Kùzu backend would
compile to openCypher. That keeps "graph queries" honest without depending on the archived
embedded DB to prove the idea.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracegraph.model import EdgeType, NormalizedTrace, Step, StepKind, StepStatus


@dataclass(frozen=True)
class StepPredicate:
    """Matches a step on any combination of name / kind / status (unset fields are wildcards)."""

    name: str | None = None
    kind: StepKind | None = None
    status: StepStatus | None = None

    def matches(self, step: Step) -> bool:
        if self.name is not None and step.name != self.name:
            return False
        if self.kind is not None and step.kind is not self.kind:
            return False
        if self.status is not None and step.status is not self.status:
            return False
        return True

    def __str__(self) -> str:
        parts = []
        if self.name is not None:
            parts.append(f"name={self.name}")
        if self.kind is not None:
            parts.append(f"kind={self.kind.value}")
        if self.status is not None:
            parts.append(f"status={self.status.value}")
        return "{" + ", ".join(parts) + "}" if parts else "{any}"


@dataclass(frozen=True)
class PathPattern:
    """A sequence of predicates matched against consecutive steps along a causal path."""

    steps: tuple[StepPredicate, ...]
    description: str = ""

    def __str__(self) -> str:
        return " → ".join(str(p) for p in self.steps)


@dataclass
class Match:
    trace_id: str
    step_ids: list[str]
    labels: list[str] = field(default_factory=list)


def _children(nt: NormalizedTrace) -> dict[str, list[str]]:
    """cause_id -> [effect_id, ...] in execution order (CAUSED_BY points effect -> cause)."""
    out: dict[str, list[str]] = {}
    for e in nt.edges_of(EdgeType.CAUSED_BY):
        out.setdefault(e.dst, []).append(e.src)
    return out


def find_matches(nt: NormalizedTrace, pattern: PathPattern) -> list[list[str]]:
    """All contiguous causal paths (parent → child …) whose steps satisfy the predicates in order."""
    if not pattern.steps:
        return []
    steps = nt.steps_by_id()
    children = _children(nt)
    preds = pattern.steps
    results: list[list[str]] = []

    def dfs(node_id: str, idx: int, acc: list[str]) -> None:
        if not preds[idx].matches(steps[node_id]):
            return
        acc = acc + [node_id]
        if idx + 1 == len(preds):
            results.append(acc)
            return
        for child in children.get(node_id, []):
            dfs(child, idx + 1, acc)

    for sid in steps:
        dfs(sid, 0, [])
    return results


def search(traces: list[NormalizedTrace], pattern: PathPattern) -> list[Match]:
    """Run a pattern over many traces; return every match with readable step labels."""
    matches: list[Match] = []
    for nt in traces:
        steps = nt.steps_by_id()
        for path in find_matches(nt, pattern):
            labels = [steps[i].name or steps[i].kind.value for i in path]
            matches.append(Match(trace_id=nt.trace.trace_id, step_ids=path, labels=labels))
    return matches


#: Named preset patterns shipped with the CLI.
PRESETS: dict[str, PathPattern] = {
    "error": PathPattern(
        (StepPredicate(status=StepStatus.ERROR),),
        "any step that errored",
    ),
    "tool-failure": PathPattern(
        (StepPredicate(kind=StepKind.TOOL, status=StepStatus.ERROR),),
        "a tool step that errored",
    ),
    "plan-then-tool-failure": PathPattern(
        (
            StepPredicate(name="plan"),
            StepPredicate(kind=StepKind.TOOL, status=StepStatus.ERROR),
        ),
        "a 'plan' step immediately followed (causally) by a failing tool",
    ),
}
