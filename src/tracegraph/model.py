"""The frozen contract: the normalized causal-graph data model.

This module is the *contract* the rest of tracegraph is built against. Two rules,
fixed here in Phase 0, must never drift:

1. **Edge direction is effect → cause.** Every causal edge points from a step to
   one of its predecessors, so traversing outbound edges walks *backward* toward
   root causes.

2. **Two layers, one vocabulary.**
   - ``CAUSED_BY`` is the *raw* layer and the system of record: it keeps **every**
     real predecessor of a step (fan-in, retries, nested tool/LLM calls). Nothing
     is dropped. Root-cause analysis reads this layer.
   - ``TREE_PARENT`` is the *derived* layer: a computed projection that picks
     exactly one parent per non-root step, yielding the single-parent forest the
     structural (AHU) diff needs. It is **not** asserted to be "the cause" — it is
     a projection parent for diffing. A step that had more than one ``CAUSED_BY``
     is flagged ``projection_lossy=True`` so the lossiness is always visible.

See ``normalize.py`` for how the derived layer is computed from the raw layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class StepKind(str, Enum):
    """What a causal-event step represents.

    Mirrors OpenInference span kinds so the OTLP adapter maps cleanly, plus ``DATA`` for
    state/data nodes. LangGraph super-steps that can't be classified more precisely default
    to ``CHAIN``.
    """

    AGENT = "AGENT"
    CHAIN = "CHAIN"
    TOOL = "TOOL"
    LLM = "LLM"
    RETRIEVER = "RETRIEVER"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    GUARDRAIL = "GUARDRAIL"
    EVALUATOR = "EVALUATOR"
    PROMPT = "PROMPT"
    UNKNOWN = "UNKNOWN"
    DATA = "DATA"


class StepSource(str, Enum):
    """How the step entered the trace (mirrors LangGraph ``metadata.source``).

    ``TASK`` is the one value that does *not* come from ``metadata.source``: it marks a step
    derived from a checkpoint's **pending writes** (one LangGraph task) rather than from a
    checkpoint itself. It is explicit provenance, not a naming convention — ``step_id`` is an
    unrestricted string, so nothing about an id may be read as evidence of how a step was made.
    """

    INPUT = "input"
    LOOP = "loop"
    UPDATE = "update"
    FORK = "fork"
    TASK = "task"


class StepStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


class EdgeOrigin(str, Enum):
    """Declared evidence behind a raw causal edge."""

    CHECKPOINT_PARENT = "checkpoint_parent"
    GRAPH_PARENT = "graph_parent"
    SPAN_LINK = "span_link"
    SPAN_PARENT_FALLBACK = "span_parent_fallback"
    LEGACY_UNKNOWN = "legacy_unknown"


class CausalFidelity(str, Enum):
    """How much of the source system's causal vocabulary survived export."""

    DECLARED_DAG = "declared_dag"
    PARENT_ONLY = "parent_only"
    LEGACY_UNKNOWN = "legacy_unknown"


class EvaluationSummary(BaseModel):
    """Body-free Phoenix/OpenInference annotation evidence."""

    name: str
    label: str | None = None
    score: float | None = None


class StepEvidence(BaseModel):
    """Allowlisted operational telemetry; never prompt/output bodies."""

    end_ts: str | None = None
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    prompt_cost: str | None = None
    completion_cost: str | None = None
    total_cost: str | None = None
    cost_currency: str | None = None
    artifact_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evaluations: list[EvaluationSummary] = Field(default_factory=list)


class DecisionEvidence(BaseModel):
    """Body-free evidence from an external pre-execution policy decision.

    It is trace metadata, never a causal edge: a preflight can constrain a run without
    proving that it caused a later outcome.
    """

    source: Literal["toolgraph_preflight"] = "toolgraph_preflight"
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    graph_generation: int = Field(ge=0)
    verdict: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")


class EdgeType(str, Enum):
    """The frozen edge vocabulary. Direction is effect → cause for causal edges."""

    CAUSED_BY = "CAUSED_BY"      # raw layer: step -> a real predecessor (keep ALL)
    TREE_PARENT = "TREE_PARENT"  # derived layer: step -> its single chosen parent
    BELONGS_TO = "BELONGS_TO"    # step -> trace
    WROTE = "WROTE"              # step -> channel (optional, flagged)
    READ = "READ"               # step -> channel (optional, flagged)


class Step(BaseModel):
    """A causal-event node: one LangGraph super-step/checkpoint or one OTLP span."""

    step_id: str
    trace_id: str
    seq: int = Field(description="Monotonic execution order within the trace.")
    ts: str | None = Field(default=None, description="ISO-8601 timestamp, when available.")
    kind: StepKind = StepKind.CHAIN
    source: StepSource = StepSource.LOOP
    name: str | None = None
    status: StepStatus = StepStatus.OK
    error_msg: str | None = None
    evidence: StepEvidence | None = None
    #: Set during normalization: True iff this step had >1 raw CAUSED_BY edge and
    #: the derived TREE_PARENT projection therefore dropped at least one real cause.
    projection_lossy: bool = False


class Edge(BaseModel):
    """A typed, directed edge. For causal edges, ``src`` is the effect and ``dst`` the cause."""

    type: EdgeType
    src: str
    dst: str
    origin: EdgeOrigin | None = None


class Trace(BaseModel):
    """One agent run / thread."""

    trace_id: str
    source_kind: str = Field(description="Which adapter produced this trace, e.g. 'langgraph'.")
    run_id: str | None = Field(
        default=None,
        description="Optional cross-system run identifier (for example, a SyncMill run_id).",
    )
    thread_id: str | None = None
    status: StepStatus = StepStatus.OK
    causal_fidelity: CausalFidelity = CausalFidelity.LEGACY_UNKNOWN
    links_preserved: bool | None = None
    decision_evidence: list[DecisionEvidence] = Field(default_factory=list)


class RawTrace(BaseModel):
    """What an adapter emits: a trace, its steps, and the RAW causal edges only.

    ``causal_edges`` must all be ``CAUSED_BY`` (effect → cause) — the system of record,
    keeping every real predecessor. Adapters never compute the derived tree; they hand a
    ``RawTrace`` to :func:`tracegraph.normalize.normalize`, which validates it and returns
    a :class:`NormalizedTrace`. Keeping the two as **distinct types** is the contract
    guard: an un-normalized trace (no ``TREE_PARENT`` layer) cannot reach ``diff``/
    ``inspect`` by accident.
    """

    trace: Trace
    steps: list[Step] = Field(default_factory=list)
    causal_edges: list[Edge] = Field(default_factory=list)
    #: Disclosures that are only knowable while reading the source (e.g. a checkpoint whose
    #: task writes could not be attributed to a node). They describe the *ingestion*, not the
    #: run, so they never reach ``NormalizedTrace`` or the artifact — the CLI prints them.
    ingest_warnings: list[str] = Field(default_factory=list)

    def steps_by_id(self) -> dict[str, Step]:
        return {s.step_id: s for s in self.steps}


class NormalizedTrace(BaseModel):
    """The portable bundle: a trace plus its steps and BOTH edge layers.

    Produced **only** by :func:`tracegraph.normalize.normalize` — never constructed
    directly by adapters. It is what ``artifact.py`` (de)serializes and what the stores
    load. The raw ``CAUSED_BY`` edges are the system of record; the derived
    ``TREE_PARENT`` edges (one per non-root step) are recorded alongside them.
    """

    trace: Trace
    steps: list[Step] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    # --- convenience views (no business logic; just typed filters) ---

    def steps_by_id(self) -> dict[str, Step]:
        return {s.step_id: s for s in self.steps}

    def edges_of(self, edge_type: EdgeType) -> list[Edge]:
        return [e for e in self.edges if e.type is edge_type]
