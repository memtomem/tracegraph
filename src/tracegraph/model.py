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
import re
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)


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

    ``TASK`` and ``INTERRUPT`` do *not* come from ``metadata.source``. ``TASK`` marks a step
    derived from a checkpoint's **pending writes** (one LangGraph task) rather than from a
    checkpoint itself. It is explicit provenance, not a naming convention — ``step_id`` is an
    unrestricted string, so nothing about an id may be read as evidence of how a step was made.

    ``INTERRUPT`` is likewise derived from pending writes: a LangGraph task that called ``interrupt()`` and
    for which the recorded history holds no resume and no result. It states what the
    checkpoints record, not that LangGraph would resume the task today — a thread re-run under
    a revised graph can finish without writing a checkpoint, leaving the record unchanged.

    ``SCHEDULED`` is a task that a compiled graph supplied at ingest schedules at the thread's
    newest checkpoint and for which that checkpoint holds no write at all: a run stopped by the
    recursion limit, killed, or held at a static breakpoint. It is LangGraph's answer *for that
    graph* — another graph can give another answer — and it does not claim the task never
    started: a task killed mid-run records nothing either. The trace header's
    ``schedule_graph`` identifies the graph.
    """

    INPUT = "input"
    LOOP = "loop"
    UPDATE = "update"
    FORK = "fork"
    TASK = "task"
    INTERRUPT = "interrupt"
    SCHEDULED = "scheduled"


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
    score: float | None = Field(default=None, allow_inf_nan=False)


class StepEvidence(BaseModel):
    """Allowlisted operational telemetry; never prompt/output bodies."""

    end_ts: str | None = None
    duration_ms: float | None = Field(default=None, allow_inf_nan=False)
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


#: Edge origins that record a declared cause, as opposed to containment (span nesting) or an
#: origin nobody recorded. Only these are ever rendered as "caused by".
EXPLICIT_ORIGINS = frozenset(
    {EdgeOrigin.CHECKPOINT_PARENT, EdgeOrigin.GRAPH_PARENT, EdgeOrigin.SPAN_LINK}
)

#: A content digest as tracegraph writes and accepts it.
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class EdgeType(str, Enum):
    """The frozen edge vocabulary. Direction is effect → cause for causal edges."""

    CAUSED_BY = "CAUSED_BY"      # raw layer: step -> a real predecessor (keep ALL)
    TREE_PARENT = "TREE_PARENT"  # derived layer: step -> its single chosen parent
    BELONGS_TO = "BELONGS_TO"    # step -> trace
    WROTE = "WROTE"              # step -> channel (optional, flagged)
    READ = "READ"               # step -> channel (optional, flagged)


#: A step field whose stored value an adapter defaulted or substituted rather than read from
#: the producer: ``kind`` and ``name`` (a span without a recognized kind or a usable name; every
#: LangGraph checkpoint step except the input checkpoint and the loop step applying it, whose
#: names are inferred from routing; an unattributed LangGraph task) and ``error_msg`` (a
#: fallback, placeholder or withheld message on an ERROR step). ``None`` alone is not this
#: signal: an unnamed LangGraph input checkpoint is a known, nameless step.
UnknownField = Literal["error_msg", "kind", "name"]
_UNKNOWN_FIELDS: TypeAdapter[list[UnknownField]] = TypeAdapter(list[UnknownField])


def _canonical_unknown_fields(value: list[UnknownField]) -> list[UnknownField]:
    if value != sorted(set(value)):
        raise ValueError("unknown_fields must be sorted and free of duplicates")
    return value


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
    #: Fields whose value is a substitute, sorted. Only an adapter that also records a trace
    #: ``capture`` sets it, so an artifact without one never claims its values are complete.
    unknown_fields: list[UnknownField] = Field(default_factory=list)

    @field_validator("unknown_fields")
    @classmethod
    def _canonical(cls, value: list[UnknownField]) -> list[UnknownField]:
        return _canonical_unknown_fields(value)


class Edge(BaseModel):
    """A typed, directed edge. For causal edges, ``src`` is the effect and ``dst`` the cause."""

    type: EdgeType
    src: str
    dst: str
    origin: EdgeOrigin | None = None


class ScheduleGraph(BaseModel):
    """The compiled graph whose schedule produced a trace's ``SCHEDULED`` steps.

    Present whenever one was supplied, even when it scheduled nothing: that is what separates
    "checked against a graph, nothing pending" from "not checked". Only a structural
    fingerprint is kept. The import reference is a local path or module name, which would make
    the same run's artifact differ from machine to machine.
    """

    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


#: Every disclosure an adapter can make about what it could not read, as a fixed code. The
#: stderr warning carries the detail; the code is what survives into the artifact, so it holds
#: no checkpoint id, task id or producer value.
CAPTURE_GAP_CODES = frozenset(
    {
        "langgraph.malformed_pending_writes",
        "langgraph.rewritten_error_channel",
        "langgraph.unapplied_command_goto",
        "langgraph.unattributed_failed_task",
        "langgraph.unattributed_interrupted_task",
        "langgraph.undecided_interrupt",
        "langgraph.undecodable_send_packets",
        "langgraph.unknown_send_target",
        "otlp.foreign_trace_link",
        "otlp.graph_parent_by_timing",
        "otlp.graph_parent_fallback",
        "otlp.unreadable_component_cost",
        "otlp.unreadable_cost_currency",
        "otlp.unreadable_timestamp",
        "otlp.unreadable_token_count",
        "otlp.unreadable_total_cost",
        "otlp.unresolved_span_link",
        "otlp.unscoped_span_link",
        "phoenix.unreadable_evaluation_score",
    }
)


class CaptureGap(BaseModel):
    #: Any well-formed code is read and kept, so a newer writer can add one without breaking a
    #: v7 reader, which must treat a code it does not know as a gap of unknown effect. Writers
    #: are held to ``CAPTURE_GAP_CODES`` by :meth:`Capture.from_counts`.
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
    count: int = Field(ge=1)


class Capture(BaseModel):
    """What the adapter that read this trace could not observe.

    Present on every trace ingested by a build that records it, with an empty ``gaps`` list
    when the read raised no trace-level disclosure; values substituted on a single step are
    recorded separately, in ``Step.unknown_fields``. Its absence means "not recorded".
    """

    #: Required, with no default: a record that omits it is malformed, not an empty one.
    gaps: list[CaptureGap]

    @field_validator("gaps")
    @classmethod
    def _canonical_gaps(cls, value: list[CaptureGap]) -> list[CaptureGap]:
        codes = [gap.code for gap in value]
        if codes != sorted(set(codes)):
            raise ValueError("capture gaps must be sorted by code with one entry per code")
        return value

    @classmethod
    def from_counts(cls, counts: dict[str, int]) -> "Capture":
        """The record an adapter writes; only codes this build defines may be emitted."""
        unknown = sorted(set(counts) - CAPTURE_GAP_CODES)
        if unknown:
            raise ValueError(f"unknown capture gap code(s) {unknown!r}")
        return cls(gaps=[CaptureGap(code=code, count=counts[code]) for code in sorted(counts)])


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
    schedule_graph: ScheduleGraph | None = None
    capture: Capture | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_optional_headers(self, handler: SerializerFunctionWrapHandler) -> dict:
        # Absent, a key is left out rather than written as null, so a trace that does not use
        # it serializes to the same bytes as before the field existed.
        data = handler(self)
        for key in ("schedule_graph", "capture"):
            if getattr(self, key) is None:
                data.pop(key, None)
        return data


def check_provenance(trace: Trace, steps: list[Step]) -> None:
    """Raise ``ValueError`` unless the provenance is what the artifact reader accepts.

    Construction validates it, but a model changed afterwards (a list extended in place, a
    header reassigned) is not revalidated, so writers and ``validate_normalized`` call this too.
    """
    capture = trace.capture
    if capture is not None:
        if not isinstance(capture, Capture):
            raise ValueError("trace capture must be a capture record")
        Capture.model_validate(capture.model_dump())
    # Every step, not only the flagged ones: None or {} would otherwise pass as "no flags".
    flagged = False
    for step in steps:
        _canonical_unknown_fields(_UNKNOWN_FIELDS.validate_python(step.unknown_fields))
        flagged = flagged or bool(step.unknown_fields)
    # Substitution flags without a capture record would be provenance an older envelope could
    # carry and an older reader would silently drop; the pair is written together or not at all.
    if capture is None and flagged:
        raise ValueError("steps carry unknown_fields but the trace has no capture record")


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
    #: run, so the text never reaches ``NormalizedTrace`` or the artifact — the CLI prints it.
    #: Each one's fixed code and count is kept in ``trace.capture``.
    ingest_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _provenance(self) -> "RawTrace":
        check_provenance(self.trace, self.steps)
        return self

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

    @model_validator(mode="after")
    def _provenance(self) -> "NormalizedTrace":
        check_provenance(self.trace, self.steps)
        return self

    # --- convenience views (no business logic; just typed filters) ---

    def steps_by_id(self) -> dict[str, Step]:
        return {s.step_id: s for s in self.steps}

    def edges_of(self, edge_type: EdgeType) -> list[Edge]:
        return [e for e in self.edges if e.type is edge_type]
