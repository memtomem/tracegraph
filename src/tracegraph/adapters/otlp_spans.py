"""Ingest OpenInference / OTLP spans into a :class:`RawTrace`.

Parses an exported OTLP trace document (the protobuf-JSON shape with
``resourceSpans[].scopeSpans[].spans[]`` emitted by the OpenTelemetry Collector or another
OTLP source) and reconstructs one trace's *raw* causal graph. One span
becomes one :class:`Step`; causal edges (``CAUSED_BY``, effect → cause) come from two
**declared** signals only — never from inferred sibling order:

* **One parent per span — the logical causal edge.** We use the OpenInference
  ``graph.node.parent_id`` (the *agent-graph* parent): a single span carrying that
  ``graph.node.id`` is trusted outright, and a loop (several do) resolves to the nearest
  *preceding* execution. If ``graph.node.parent_id`` is declared but resolves to no single
  in-trace span, we fall back to the structural ``parentSpanId`` — and raise if there is none
  (see below). When no ``graph.node.parent_id`` is declared, ``parentSpanId`` is the parent.
  Span-tree nesting is treated as structural **containment, not an independent cause** — we
  deliberately do not record both, which would flag almost every span ``projection_lossy``
  for mere hierarchy rather than real data-flow fan-in.
* **Span links — zero or more additional causes.** Each OTLP ``link`` that explicitly names
  *this* trace and an in-trace span is a declared cause; a missing or foreign ``traceId`` is
  never silently treated as local. We trust declared links regardless of timestamp (seq is
  re-derived by topological order, and a genuine cycle raises). Links are where span traces
  become a true multi-parent DAG — a span with a parent *and* a link is flagged
  ``projection_lossy`` because the single-parent tree view must drop one of those causes.

Honesty guards, mirrored from the LangGraph adapter:

* **Never fabricate a root.** A ``parentSpanId`` pointing outside the ingested trace means
  a partial/corrupt export; we raise rather than silently reparent the span to a new root.
  Likewise, a declared ``graph.node.parent_id`` that cannot be resolved and has no
  ``parentSpanId`` fallback raises — we never root a span whose declared parent we can't honor.
* **Never invent causality.** Sequentially-adjacent sibling spans are *not* linked — only
  the declared parent and explicit links become edges, because only those are asserted by
  the source data. Guessing "B ran after A so A caused B" is exactly the fabrication the
  raw/derived contract exists to prevent.

Documented MVP assumptions:

* ``traceId``/``spanId`` are opaque string ids (OTLP/JSON hex encoding) — not decoded.
* ``seq`` is a **topological order** of the reconstructed causal DAG (every cause precedes
  its effect), tie-broken by start time so it tracks real time wherever the DAG leaves the
  order free. This holds even under clock skew — a child stamped *before* its parent keeps
  its declared parent edge rather than being silently dropped into a fabricated root —
  because we trust declared structure over timestamps. A genuine cycle in the declared
  parent/link edges (contradictory causality) raises.
"""

from __future__ import annotations

import heapq
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Iterator

from tracegraph.input_validation import loads, object_field

from tracegraph.model import (
    CausalFidelity,
    DecisionEvidence,
    Edge,
    EdgeOrigin,
    EdgeType,
    EvaluationSummary,
    RawTrace,
    SHA256_DIGEST,
    Step,
    StepEvidence,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)

_SPAN_KIND_ATTR = "openinference.span.kind"
_GRAPH_NODE_ID = "graph.node.id"
_GRAPH_NODE_PARENT_ID = "graph.node.parent_id"
_SYNCMILL_RUN_ID = "syncmill.run_id"
_TOOLGRAPH_PREFLIGHT_DIGEST = "toolgraph.preflight.artifact_digest"
_TOOLGRAPH_GRAPH_GENERATION = "toolgraph.graph_generation"
_TOOLGRAPH_PREFLIGHT_VERDICT = "toolgraph.preflight.verdict"

#: OpenInference span kinds that map cleanly onto a :class:`StepKind`. Anything else
#: (EMBEDDING, RERANKER, GUARDRAIL, EVALUATOR, or absent) defaults to ``CHAIN`` — see
#: the ``StepKind`` docstring: spans that can't be classified more precisely are chains.
_KIND_MAP = {
    "AGENT": StepKind.AGENT,
    "CHAIN": StepKind.CHAIN,
    "TOOL": StepKind.TOOL,
    "LLM": StepKind.LLM,
    "RETRIEVER": StepKind.RETRIEVER,
    "EMBEDDING": StepKind.EMBEDDING,
    "RERANKER": StepKind.RERANKER,
    "GUARDRAIL": StepKind.GUARDRAIL,
    "EVALUATOR": StepKind.EVALUATOR,
    "PROMPT": StepKind.PROMPT,
    "UNKNOWN": StepKind.UNKNOWN,
}

def _first(d: dict, *names: str, default: Any = None) -> Any:
    """Return the first present, non-None key from ``names`` (camelCase/snake_case tolerant)."""
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return default


def _attr_value(v: Any) -> Any:
    """Unwrap an OTLP ``AnyValue`` (``{"stringValue": ...}``) to a Python scalar."""
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "string_value"):
        if key in v:
            return v[key]
    for key in ("intValue", "int_value"):
        if key in v:  # protobuf-JSON encodes int64 as a string
            raw = v[key]
            if isinstance(raw, bool):
                return raw  # `int(True) == 1` would hide that the value was a boolean
            if isinstance(raw, float) and not raw.is_integer():
                return raw  # `int(1.5) == 1` would hand validation a plausible count
            try:
                return int(raw)
            except (TypeError, ValueError, OverflowError):
                return raw
    for key in ("boolValue", "bool_value", "doubleValue", "double_value"):
        if key in v:
            return v[key]
    return v  # array/kvlist values: hand back as-is (not needed by the causal model)


def _attributes(span: dict) -> dict[str, Any]:
    raw = span.get("attributes")
    if raw is None:
        raw = []
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, list):
        raise ValueError("attributes must be an object or array")
    out: dict[str, Any] = {}
    for a in raw:
        if not isinstance(a, dict):
            raise ValueError("attributes[] must be an object")
        key = a.get("key")
        if key is not None and not isinstance(key, str):
            raise ValueError("attributes[].key must be a string")
        if key is not None:
            out[key] = _attr_value(a.get("value"))
    return out


def _iter_spans(document: dict) -> Iterator[dict]:
    # Malformed nesting raises a clean ValueError rather than an AttributeError deep in
    # a caller — the CLI's load boundary only translates OSError/ValueError/KeyError.
    for rs in _span_array(document, "resourceSpans", "resource_spans"):
        if not isinstance(rs, dict):
            raise ValueError("OTLP resourceSpans entries must be objects")
        for ss in _span_array(rs, "scopeSpans", "scope_spans"):
            if not isinstance(ss, dict):
                raise ValueError("OTLP scopeSpans entries must be objects")
            for span in _span_array(ss, "spans"):
                if not isinstance(span, dict):
                    raise ValueError("OTLP spans entries must be objects")
                yield span


def _span_array(container: dict, *keys: str) -> list:
    """Fetch a spans-shaped array field, treating only missing/None as empty.

    Any other non-array value (including falsey ones like ``{}`` or ``0``) is malformed
    input and raises — an ``or []`` would silently swallow it as "no spans".
    """
    value = _first(container, *keys, default=None)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"OTLP {keys[0]} must be an array")
    return value


def _exception_message(span: dict) -> str | None:
    for ev in _span_array(span, "events"):
        if not isinstance(ev, dict):
            raise ValueError("events[] must be an object")
        attrs = _attributes(ev)
        if ev.get("name") == "exception":
            msg = attrs.get("exception.message") or attrs.get("exception.type")
            if msg:
                return str(msg)
    return None


def _status(span: dict) -> tuple[StepStatus, str | None]:
    st = object_field(span.get("status"), "status")
    code = st.get("code")
    if isinstance(code, bool):
        # `True == 1` would otherwise read as OK.
        raise ValueError("status.code must be OK, ERROR, or UNSET")
    exception = _exception_message(span)
    if code in (1, "STATUS_CODE_OK", "OK"):
        return StepStatus.OK, None
    if code in (2, "STATUS_CODE_ERROR", "ERROR"):
        return StepStatus.ERROR, st.get("message") or exception or "error"
    if code not in (0, "STATUS_CODE_UNSET", "UNSET", None):
        raise ValueError("status.code must be OK, ERROR, or UNSET")
    if exception is not None:
        return StepStatus.ERROR, exception
    return StepStatus.UNSET, None


def _parse_nanos(value: Any) -> int | None:
    """Nanoseconds since the epoch, or None when ``value`` is missing or cannot be a time.

    Garbage text and non-finite or out-of-range numbers are all "unavailable" rather than an
    error: timestamps order and time a trace but never decide its causality. The caller
    discloses how many it could not read.
    """
    try:
        nanos = int(value)
        datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return nanos


def _iso_ts(start_nano: Any) -> str | None:
    nanos = _parse_nanos(start_nano)
    if nanos is None:
        return None
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).isoformat()


def _as_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or (isinstance(value, float) and not value.is_integer()):
        return None
    return parsed


def _as_decimal(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return format(parsed, "f") if parsed.is_finite() and parsed >= 0 else None


def _as_currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    return normalized if re.fullmatch(r"[A-Z]{3,8}", normalized) else None


def _first_readable(attrs: dict[str, Any], read: Any, *names: str) -> Any:
    for name in names:
        parsed = read(attrs.get(name))
        if parsed is not None:
            return parsed
    return None


def _unreadable(attrs: dict[str, Any], read: Any, *names: str) -> bool:
    """The producer sent a value under one of ``names`` and none of them can be read."""
    return _first_readable(attrs, read, *names) is None and any(
        attrs.get(name) is not None for name in names
    )


_TOKEN_ATTRS = (
    ("llm.token_count.prompt", "llm.token_count.input"),
    ("llm.token_count.completion", "llm.token_count.output"),
    ("llm.token_count.total",),
)
_COST_ATTRS = (
    ("llm.cost.prompt", "llm.cost.input"),
    ("llm.cost.completion", "llm.cost.output"),
    ("llm.cost.total",),
)
_CURRENCY_ATTR = "llm.cost.currency"
#: Stored in ``total_cost`` when the producer sent a total cost that cannot be read. It is not a
#: decimal, so analysis withholds the run's total instead of summing the rest into a figure that
#: is knowably too low. Fixed text: the producer's own value never reaches the artifact.
UNREADABLE_COST = "unreadable"


def _unreadable_telemetry(attrs: dict[str, Any]) -> dict[str, set[str]]:
    """Which kinds of telemetry this span sent but tracegraph cannot read, and under which of
    our fixed attribute names (never the producer's value)."""
    groups = (
        [("token count", _as_count, names) for names in _TOKEN_ATTRS]
        + [("prompt or completion cost", _as_decimal, names) for names in _COST_ATTRS[:2]]
        + [("total cost", _as_decimal, _COST_ATTRS[2])]
        + [("cost currency", _as_currency, (_CURRENCY_ATTR,))]
    )
    found: dict[str, set[str]] = {}
    for kind, read, names in groups:
        if _unreadable(attrs, read, *names):
            found.setdefault(kind, set()).update(n for n in names if attrs.get(n) is not None)
    return found


def _digest_attr(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not SHA256_DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


def _evidence(
    span: dict, attrs: dict[str, Any], evaluation_channel: str | None = None
) -> StepEvidence | None:
    start = _parse_nanos(_first(span, "startTimeUnixNano", "start_time_unix_nano"))
    end = _first(span, "endTimeUnixNano", "end_time_unix_nano")
    end_nanos = _parse_nanos(end)
    duration_ms: float | None = None
    if start is not None and end_nanos is not None:
        duration_ms = round((end_nanos - start) / 1_000_000, 6)

    evaluations = [
        EvaluationSummary.model_validate(item)
        for item in ((span.get(evaluation_channel) or []) if evaluation_channel else [])
        if isinstance(item, dict) and item.get("name")
    ]
    evaluations.sort(key=lambda item: (item.name, item.label or "", item.score or 0.0))
    evidence = StepEvidence(
        end_ts=_iso_ts(end),
        duration_ms=duration_ms,
        prompt_tokens=_first_readable(attrs, _as_count, *_TOKEN_ATTRS[0]),
        completion_tokens=_first_readable(attrs, _as_count, *_TOKEN_ATTRS[1]),
        total_tokens=_first_readable(attrs, _as_count, *_TOKEN_ATTRS[2]),
        prompt_cost=_first_readable(attrs, _as_decimal, *_COST_ATTRS[0]),
        completion_cost=_first_readable(attrs, _as_decimal, *_COST_ATTRS[1]),
        total_cost=(
            _first_readable(attrs, _as_decimal, *_COST_ATTRS[2])
            or (UNREADABLE_COST if _unreadable(attrs, _as_decimal, *_COST_ATTRS[2]) else None)
        ),
        cost_currency=_as_currency(attrs.get(_CURRENCY_ATTR)),
        artifact_digest=_digest_attr(attrs, "syncmill.artifact_digest"),
        evaluations=evaluations,
    )
    scalar_values = evidence.model_dump(exclude={"evaluations"}).values()
    return evidence if evaluations or any(value is not None for value in scalar_values) else None


class OTLPSpanAdapter:
    """Reconstruct one trace's raw causal graph from an OTLP/OpenInference span export."""

    def __init__(
        self,
        document: dict,
        *,
        source_kind: str = "otlp",
        prefer_graph_parent: bool = True,
        links_as_causes: bool = True,
        include_error_messages: bool = True,
        preserve_unset_status: bool = False,
        causal_fidelity: CausalFidelity | None = None,
        links_preserved: bool | None = None,
        evaluation_channel: str | None = None,
    ) -> None:
        self._doc = document
        #: Span key carrying pre-sanitized evaluation summaries. Only an adapter that builds
        #: the span dicts itself (Phoenix) sets it; raw OTLP input never supplies evaluations.
        self._evaluation_channel = evaluation_channel
        self._source_kind = source_kind
        #: Prefer ``graph.node.parent_id`` (logical agent graph) over ``parentSpanId``.
        self._prefer_graph_parent = prefer_graph_parent
        #: Treat OTLP span links as additional causes (the multi-parent fan-in source).
        self._links_as_causes = links_as_causes
        self._include_error_messages = include_error_messages
        #: Keep UNSET (without exception evidence, see ``_status``) instead of reading it as OK.
        #: Off for OTLP: UNSET is OTel's default status and SyncMill leaves successful spans
        #: unset, which ``gate-failure-after-success`` needs as ``ok``. Phoenix turns it on.
        self._preserve_unset_status = preserve_unset_status
        self._causal_fidelity = causal_fidelity or (
            CausalFidelity.DECLARED_DAG if links_as_causes else CausalFidelity.PARENT_ONLY
        )
        self._links_preserved = links_as_causes if links_preserved is None else links_preserved
        self._spans_by_trace: dict[str, list[dict]] | None = None

    @classmethod
    def from_json(cls, text: str, **kwargs: Any) -> "OTLPSpanAdapter":
        try:
            payload = loads(text)
            documents = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            # The Collector file exporter writes one top-level TracesData object per line.
            documents = [loads(line) for line in text.splitlines() if line.strip()]
        return cls.from_documents(documents, **kwargs)

    @classmethod
    def from_documents(cls, documents: list[Any], **kwargs: Any) -> "OTLPSpanAdapter":
        """Merge already-parsed TracesData objects (one per Collector JSONL line) into one adapter."""
        if not documents or not all(isinstance(item, dict) for item in documents):
            raise ValueError("OTLP input must contain one or more JSON trace objects")
        merged: dict[str, Any] = {"resourceSpans": []}
        for document in documents:
            # _span_array validates the container BEFORE merging, so e.g.
            # {"resourceSpans": 1} is a clean ValueError, not a TypeError from extend().
            merged["resourceSpans"].extend(
                _span_array(document, "resourceSpans", "resource_spans")
            )
        return cls(merged, **kwargs)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "OTLPSpanAdapter":
        return cls.from_json(Path(path).read_text(encoding="utf-8"), **kwargs)

    # --- TraceAdapter ---

    def discover(self) -> list[str]:
        """Distinct ``traceId``s present in the document, in first-seen order."""
        return list(self._grouped())

    def ingest(self, trace_id: str) -> RawTrace:
        by_id = self._spans_of(trace_id)
        parent_span = self._structural_parents(trace_id, by_id)
        depth = self._depths(parent_span)
        attrs = {sid: _attributes(by_id[sid]) for sid in by_id}
        run_id = self._run_id(trace_id, attrs)

        # prerank is a deterministic *temporal* pre-order (real start time, then span-tree
        # depth and spanId only as tiebreaks). It is used solely to decide which spans
        # count as "preceding" when resolving a logical parent or a link cause — NOT as the
        # final seq, because clock skew could otherwise order a real cause after its effect.
        prerank = {
            sid: i
            for i, sid in enumerate(
                sorted(by_id, key=lambda s: (self._start_nano(by_id[s]), depth[s], s))
            )
        }

        # Reconstruct the raw causes (effect -> [causes]) from declared signals only, then
        # assign seq as a topological order of that DAG so a cause always precedes its
        # effect by construction (validate_raw can never reject our own ordering). A genuine
        # cycle in the declared parent/link edges raises rather than silently lying.
        # Spans whose start time is missing or unreadable sort as time 0 in prerank. That is
        # fine as a tiebreak for ordering, but it is not evidence of which execution came
        # first, so it must never pick a causal parent.
        untimed = {
            sid
            for sid, span in by_id.items()
            if _parse_nanos(_first(span, "startTimeUnixNano", "start_time_unix_nano")) is None
        }
        causes, origins = self._causes(by_id, attrs, prerank, parent_span, untimed)
        seq = self._topo_seq(by_id, causes, prerank)

        steps: list[Step] = []
        for sid in by_id:
            span = by_id[sid]
            status, err = _status(span)
            if status is StepStatus.UNSET and not self._preserve_unset_status:
                status = StepStatus.OK
            steps.append(
                Step(
                    step_id=sid,
                    trace_id=trace_id,
                    seq=seq[sid],
                    ts=_iso_ts(_first(span, "startTimeUnixNano", "start_time_unix_nano")),
                    kind=_KIND_MAP.get(str(attrs[sid].get(_SPAN_KIND_ATTR, "")).upper(), StepKind.CHAIN),
                    source=StepSource.LOOP,  # spans carry no LangGraph source semantics
                    name=span.get("name"),
                    status=status,
                    error_msg=err if self._include_error_messages else None,
                    evidence=_evidence(span, attrs[sid], self._evaluation_channel),
                )
            )
        steps.sort(key=lambda s: s.seq)

        edges = [
            Edge(
                type=EdgeType.CAUSED_BY,
                src=effect,
                dst=cause,
                origin=origins[(effect, cause)],
            )
            for effect in sorted(causes, key=lambda s: prerank[s])
            for cause in sorted(causes[effect], key=lambda c: prerank[c])
        ]
        any_error = any(s.status is StepStatus.ERROR for s in steps)
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            run_id=run_id,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
            causal_fidelity=self._causal_fidelity,
            links_preserved=self._links_preserved,
            decision_evidence=self._decision_evidence(trace_id, attrs),
        )
        unreadable = sum(
            1
            for span in by_id.values()
            if any(
                value is not None and _parse_nanos(value) is None
                for value in (
                    _first(span, "startTimeUnixNano", "start_time_unix_nano"),
                    _first(span, "endTimeUnixNano", "end_time_unix_nano"),
                )
            )
        )
        warnings = (
            [
                f"{unreadable} span(s) carry a start or end timestamp that cannot be read as a "
                "time; that timestamp, and any duration derived from it, is reported as "
                "unavailable."
            ]
            if unreadable
            else []
        )
        dropped = [_unreadable_telemetry(attrs[sid]) for sid in by_id]
        for kind, consequence in (
            ("token count", "that count is reported as unavailable"),
            ("prompt or completion cost", "that cost is reported as unavailable"),
            ("total cost", "the run's total cost is withheld rather than understated"),
            ("cost currency", "that cost is reported without a currency"),
        ):
            count = sum(1 for found in dropped if kind in found)
            if count:
                names = ", ".join(sorted(set().union(*(found.get(kind, set()) for found in dropped))))
                warnings.append(
                    f"{count} span(s) carry a {kind} that cannot be read ({names}); {consequence}."
                )
        return RawTrace(trace=trace, steps=steps, causal_edges=edges, ingest_warnings=warnings)

    # --- helpers ---

    @staticmethod
    def _run_id(trace_id: str, attrs: dict[str, dict[str, Any]]) -> str | None:
        """Return one consistent optional SyncMill run id for the trace.

        ``run_id`` is common correlation metadata, not a causal signal.  We retain only this
        allowlisted value and continue to discard all other vendor attributes.  A partially
        annotated trace is acceptable, but two different declared values are not.
        """
        values: set[str] = set()
        for span_attrs in attrs.values():
            if _SYNCMILL_RUN_ID not in span_attrs:
                continue
            value = span_attrs[_SYNCMILL_RUN_ID]
            if (
                not isinstance(value, str)
                or not value
                or any(ch.isspace() for ch in value)
            ):
                raise ValueError(
                    f"trace {trace_id!r} has an empty, whitespace-containing, or non-string "
                    "syncmill.run_id"
                )
            values.add(value)
        if len(values) > 1:
            raise ValueError(f"trace {trace_id!r} has conflicting syncmill.run_id values")
        return next(iter(values), None)

    @staticmethod
    def _decision_evidence(
        trace_id: str, attrs: dict[str, dict[str, Any]]
    ) -> list[DecisionEvidence]:
        """Read one consistent, body-free Toolgraph preflight reference."""
        records: set[tuple[str, int, str]] = set()
        partial = False
        for span_attrs in attrs.values():
            has_digest = _TOOLGRAPH_PREFLIGHT_DIGEST in span_attrs
            has_generation = _TOOLGRAPH_GRAPH_GENERATION in span_attrs
            has_verdict = _TOOLGRAPH_PREFLIGHT_VERDICT in span_attrs
            if not has_digest and not has_generation and not has_verdict:
                continue
            if not (has_digest and has_generation and has_verdict):
                partial = True
                continue
            digest = span_attrs[_TOOLGRAPH_PREFLIGHT_DIGEST]
            generation = span_attrs[_TOOLGRAPH_GRAPH_GENERATION]
            verdict = span_attrs[_TOOLGRAPH_PREFLIGHT_VERDICT]
            if not isinstance(digest, str) or not SHA256_DIGEST.fullmatch(digest):
                raise ValueError(f"trace {trace_id!r} has an invalid Toolgraph preflight digest")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError(f"trace {trace_id!r} has an invalid Toolgraph graph generation")
            if not isinstance(verdict, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", verdict):
                raise ValueError(f"trace {trace_id!r} has an invalid Toolgraph preflight verdict")
            records.add((digest, generation, verdict))
        if partial:
            raise ValueError(
                f"trace {trace_id!r} must declare Toolgraph preflight digest, graph generation, and verdict together"
            )
        if len(records) > 1:
            raise ValueError(f"trace {trace_id!r} has conflicting Toolgraph preflight evidence")
        return [
            DecisionEvidence(
                artifact_digest=digest,
                graph_generation=generation,
                verdict=verdict,
            )
            for digest, generation, verdict in sorted(records)
        ]

    @staticmethod
    def _start_nano(span: dict) -> int:
        nanos = _parse_nanos(_first(span, "startTimeUnixNano", "start_time_unix_nano"))
        return 0 if nanos is None else nanos

    def _grouped(self) -> dict[str, list[dict]]:
        """Spans grouped by ``traceId`` in first-seen order, built once per adapter.

        Grouping only — per-trace validation (missing/duplicate spanId) stays in
        :meth:`_spans_of` so a malformed trace fails when *it* is ingested, not when a
        sibling trace in the same document is.
        """
        if self._spans_by_trace is None:
            grouped: dict[str, list[dict]] = {}
            for span in _iter_spans(self._doc):
                tid = _first(span, "traceId", "trace_id")
                if tid is None:
                    # Skipping it would drop the span from every trace with no error, and
                    # its children would then fail with a misleading "parent not in trace"
                    # message pointing at the wrong span. A missing spanId already raises;
                    # a missing traceId is the same class of corrupt export.
                    sid = _first(span, "spanId", "span_id")
                    raise ValueError(
                        f"span {sid!r} has no traceId; the export is partial or corrupt"
                        if sid is not None
                        else "a span has neither traceId nor spanId; the export is corrupt"
                    )
                if not isinstance(tid, str) or not tid:
                    raise ValueError(f"traceId must be a non-empty string, got {tid!r}")
                grouped.setdefault(tid, []).append(span)
            self._spans_by_trace = grouped
        return self._spans_by_trace

    def _spans_of(self, trace_id: str) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        for span in self._grouped().get(trace_id, []):
            sid = _first(span, "spanId", "span_id")
            if sid is None:
                raise ValueError(f"trace {trace_id!r} has a span with no spanId")
            if not isinstance(sid, str) or not sid:
                raise ValueError(f"trace {trace_id!r} has a spanId that is not a non-empty string")
            if sid in by_id:
                raise ValueError(f"duplicate spanId {sid!r} in trace {trace_id!r}")
            by_id[sid] = span
        if not by_id:
            raise KeyError(f"no spans for trace_id {trace_id!r}")
        return by_id

    @staticmethod
    def _structural_parents(trace_id: str, by_id: dict[str, dict]) -> dict[str, str | None]:
        """``spanId -> parentSpanId`` (None for roots), refusing dangling parents."""
        parents: dict[str, str | None] = {}
        for sid, span in by_id.items():
            pid = _first(span, "parentSpanId", "parent_span_id")
            if pid == "":
                pid = None  # protobuf-JSON writes an absent parent as the empty string
            if pid is not None and not isinstance(pid, str):
                # `0`, `[]` or `false` used to read as "no parent", fabricating a root for a
                # span whose declared parent is merely malformed.
                raise ValueError(f"span {sid!r} has a parentSpanId that is not a string")
            if pid is not None and pid not in by_id:
                raise ValueError(
                    f"span {sid!r} has parentSpanId {pid!r} not in trace {trace_id!r}; the "
                    "export is partial/corrupt — tracegraph will not fabricate a root for it"
                )
            parents[sid] = pid
        return parents

    @staticmethod
    def _depths(parent_span: dict[str, str | None]) -> dict[str, int]:
        # Walk each span's parent chain only until a span with a known depth, then unwind —
        # every span's depth is computed exactly once (O(n) overall, not O(n·depth)).
        depth: dict[str, int] = {}
        for sid in parent_span:
            chain: list[str] = []
            on_chain: set[str] = set()
            cur: str | None = sid
            while cur is not None and cur not in depth:
                if cur in on_chain:
                    raise ValueError(f"cycle in parentSpanId chain at span {cur!r}")
                chain.append(cur)
                on_chain.add(cur)
                cur = parent_span[cur]
            base = 0 if cur is None else depth[cur] + 1
            for offset, node in enumerate(reversed(chain)):
                depth[node] = base + offset
        return depth

    def _causes(
        self,
        by_id: dict[str, dict],
        attrs: dict[str, dict],
        prerank: dict[str, int],
        parent_span: dict[str, str | None],
        untimed: set[str],
    ) -> tuple[dict[str, list[str]], dict[tuple[str, str], EdgeOrigin]]:
        """Reconstruct ``effect -> [causes]`` from declared signals: one parent + links."""
        node_index: dict[str, list[str]] = {}
        if self._prefer_graph_parent:
            for sid in sorted(by_id, key=lambda s: prerank[s]):  # prerank order
                nid = attrs[sid].get(_GRAPH_NODE_ID)
                if nid is not None:
                    node_index.setdefault(str(nid), []).append(sid)

        causes: dict[str, list[str]] = {sid: [] for sid in by_id}
        seen: set[tuple[str, str]] = set()
        origins: dict[tuple[str, str], EdgeOrigin] = {}

        def add(effect: str, cause: str, origin: EdgeOrigin) -> None:
            if cause == effect or (effect, cause) in seen:
                return  # self-edges and duplicates would both break validate_raw
            seen.add((effect, cause))
            causes[effect].append(cause)
            origins[(effect, cause)] = origin

        for sid in by_id:
            parent, parent_origin = self._resolve_parent(
                sid, attrs, node_index, prerank, parent_span, untimed
            )
            if parent is not None:
                add(sid, parent, parent_origin)
            if self._links_as_causes:
                own_trace = _first(by_id[sid], "traceId", "trace_id")
                for link in _span_array(by_id[sid], "links"):
                    # A link is a declared cause only if it explicitly names THIS trace and an
                    # in-trace span. A missing/foreign traceId is NOT silently treated as local
                    # (that would fabricate causality). Declared links are trusted regardless of
                    # timestamp — topo ordering sequences them and a real cycle raises.
                    if not isinstance(link, dict):
                        raise ValueError("OTLP links entries must be objects")
                    link_trace = _first(link, "traceId", "trace_id")
                    lsid = _first(link, "spanId", "span_id")
                    if any(value is not None and not isinstance(value, str) for value in (link_trace, lsid)):
                        raise ValueError("links[].traceId and spanId must be strings")
                    if link_trace != own_trace:
                        continue
                    if lsid in by_id:
                        add(sid, lsid, EdgeOrigin.SPAN_LINK)
        return causes, origins

    def _resolve_parent(
        self,
        sid: str,
        attrs: dict[str, dict],
        node_index: dict[str, list[str]],
        prerank: dict[str, int],
        parent_span: dict[str, str | None],
        untimed: set[str],
    ) -> tuple[str | None, EdgeOrigin]:
        """The single chosen parent cause: the logical graph parent if resolvable, else the
        declared span parent.

        Resolving ``graph.node.parent_id`` to a span never gates on the clock (which would
        silently drop a real parent under skew or a timestamp tie):

        * a single span carries that ``graph.node.id`` → trust it outright (unambiguous);
        * several do (a loop) → pick the nearest *preceding* execution, but only when this
          span and every candidate have a readable start time; otherwise there is no
          evidence of order and the parent is ambiguous.

        If ``graph.node.parent_id`` is declared but resolves to no in-trace span, or is
        ambiguous (several candidates, none preceding), we fall back to the structural
        ``parentSpanId`` — and if there is no ``parentSpanId`` either, we **raise** rather than
        fabricate a root for a declared parent we cannot honor (mirroring the dangling-parent
        guard). When no ``graph.node.parent_id`` is declared, ``parentSpanId`` is the parent;
        ``None`` only when the span truly has no declared parent of either kind.
        """
        pid = parent_span.get(sid)
        if self._prefer_graph_parent:
            pnode = attrs[sid].get(_GRAPH_NODE_PARENT_ID)
            if pnode is not None:
                candidates = [c for c in node_index.get(str(pnode), []) if c != sid]
                if len(candidates) == 1:
                    return candidates[0], EdgeOrigin.GRAPH_PARENT
                timed = sid not in untimed and not any(c in untimed for c in candidates)
                preceding = [c for c in candidates if timed and prerank[c] < prerank[sid]]
                if preceding:
                    return max(preceding, key=lambda c: prerank[c]), EdgeOrigin.GRAPH_PARENT
                if pid is None:
                    detail = "no in-trace span" if not candidates else "multiple ambiguous spans"
                    raise ValueError(
                        f"span {sid!r} declares graph.node.parent_id={pnode!r} that resolves to "
                        f"{detail} and has no parentSpanId fallback; tracegraph will not "
                        "fabricate a root for a declared logical parent it cannot honor"
                    )
        return pid, EdgeOrigin.SPAN_PARENT_FALLBACK

    @staticmethod
    def _topo_seq(
        by_id: dict[str, dict],
        causes: dict[str, list[str]],
        prerank: dict[str, int],
    ) -> dict[str, int]:
        """seq as a topological order of the causal DAG: every cause is numbered before its
        effects. Ties break by ``prerank`` so the order tracks real time where the DAG
        leaves it free. A node that never reaches in-degree 0 means the declared parent/link
        edges form a causal cycle (contradictory causality) — so we raise instead of lying.
        """
        effects_of: dict[str, list[str]] = {sid: [] for sid in by_id}
        indegree: dict[str, int] = dict.fromkeys(by_id, 0)
        for effect, cs in causes.items():
            for cause in cs:
                effects_of[cause].append(effect)
                indegree[effect] += 1

        ready = [(prerank[sid], sid) for sid in by_id if indegree[sid] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            _, sid = heapq.heappop(ready)
            order.append(sid)
            for effect in effects_of[sid]:
                indegree[effect] -= 1
                if indegree[effect] == 0:
                    heapq.heappush(ready, (prerank[effect], effect))

        if len(order) != len(by_id):
            stuck = sorted(s for s in by_id if indegree[s] > 0)
            raise ValueError(
                f"causal cycle among spans {stuck[:5]} in trace; the declared parent/link "
                "edges are not acyclic (a cause cannot also be a descendant of its effect)"
            )
        return {sid: i for i, sid in enumerate(order)}
