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

from tracegraph.model import (
    CausalFidelity,
    DecisionEvidence,
    Edge,
    EdgeOrigin,
    EdgeType,
    EvaluationSummary,
    RawTrace,
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

_ERROR_CODES = {2, "STATUS_CODE_ERROR", "ERROR"}


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
            try:
                return int(v[key])
            except (TypeError, ValueError):
                return v[key]
    for key in ("boolValue", "bool_value", "doubleValue", "double_value"):
        if key in v:
            return v[key]
    return v  # array/kvlist values: hand back as-is (not needed by the causal model)


def _attributes(span: dict) -> dict[str, Any]:
    raw = span.get("attributes") or []
    if isinstance(raw, dict):
        return dict(raw)
    out: dict[str, Any] = {}
    for a in raw:
        key = a.get("key")
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
    for ev in span.get("events") or []:
        if ev.get("name") == "exception":
            attrs = {
                a.get("key"): _attr_value(a.get("value")) for a in ev.get("attributes") or []
            }
            msg = attrs.get("exception.message") or attrs.get("exception.type")
            if msg:
                return str(msg)
    return None


def _status(span: dict) -> tuple[StepStatus, str | None]:
    st = span.get("status") or {}
    code = st.get("code")
    exception = _exception_message(span)
    if code in _ERROR_CODES or exception is not None:
        return StepStatus.ERROR, st.get("message") or exception or "error"
    if code in (0, "STATUS_CODE_UNSET", "UNSET", None):
        return StepStatus.UNSET, None
    return StepStatus.OK, None


def _iso_ts(start_nano: Any) -> str | None:
    try:
        nanos = int(start_nano)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).isoformat()


def _int_attr(attrs: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = attrs.get(name)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed < 0 or (isinstance(value, float) and not value.is_integer()):
            continue
        return parsed
    return None


def _decimal_attr(attrs: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = attrs.get(name)
        if value is None or not isinstance(value, (int, float, str)):
            continue
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            continue
        if parsed.is_finite() and parsed >= 0:
            return format(parsed, "f")
    return None


def _currency_attr(attrs: dict[str, Any]) -> str | None:
    value = attrs.get("llm.cost.currency")
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    return normalized if re.fullmatch(r"[A-Z]{3,8}", normalized) else None


def _digest_attr(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be a lowercase sha256 digest")
    return value


def _evidence(span: dict, attrs: dict[str, Any]) -> StepEvidence | None:
    start = _first(span, "startTimeUnixNano", "start_time_unix_nano")
    end = _first(span, "endTimeUnixNano", "end_time_unix_nano")
    duration_ms: float | None = None
    try:
        if start is not None and end is not None:
            duration_ms = round((int(end) - int(start)) / 1_000_000, 6)
    except (TypeError, ValueError):
        duration_ms = None

    evaluations = [
        EvaluationSummary.model_validate(item)
        for item in span.get("_tracegraph_evaluations", [])
        if isinstance(item, dict) and item.get("name")
    ]
    evaluations.sort(key=lambda item: (item.name, item.label or "", item.score or 0.0))
    evidence = StepEvidence(
        end_ts=_iso_ts(end),
        duration_ms=duration_ms,
        prompt_tokens=_int_attr(attrs, "llm.token_count.prompt", "llm.token_count.input"),
        completion_tokens=_int_attr(
            attrs, "llm.token_count.completion", "llm.token_count.output"
        ),
        total_tokens=_int_attr(attrs, "llm.token_count.total"),
        prompt_cost=_decimal_attr(attrs, "llm.cost.prompt", "llm.cost.input"),
        completion_cost=_decimal_attr(attrs, "llm.cost.completion", "llm.cost.output"),
        total_cost=_decimal_attr(attrs, "llm.cost.total"),
        cost_currency=_currency_attr(attrs),
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
    ) -> None:
        self._doc = document
        self._source_kind = source_kind
        #: Prefer ``graph.node.parent_id`` (logical agent graph) over ``parentSpanId``.
        self._prefer_graph_parent = prefer_graph_parent
        #: Treat OTLP span links as additional causes (the multi-parent fan-in source).
        self._links_as_causes = links_as_causes
        self._include_error_messages = include_error_messages
        self._preserve_unset_status = preserve_unset_status
        self._causal_fidelity = causal_fidelity or (
            CausalFidelity.DECLARED_DAG if links_as_causes else CausalFidelity.PARENT_ONLY
        )
        self._links_preserved = links_as_causes if links_preserved is None else links_preserved
        self._spans_by_trace: dict[str, list[dict]] | None = None

    @classmethod
    def from_json(cls, text: str, **kwargs: Any) -> "OTLPSpanAdapter":
        try:
            payload = json.loads(text)
            documents = payload if isinstance(payload, list) else [payload]
        except json.JSONDecodeError:
            # The Collector file exporter writes one top-level TracesData object per line.
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
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
        causes, origins = self._causes(by_id, attrs, prerank, parent_span)
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
                    evidence=_evidence(span, attrs[sid]),
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
        return RawTrace(trace=trace, steps=steps, causal_edges=edges)

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
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
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
        try:
            return int(_first(span, "startTimeUnixNano", "start_time_unix_nano"))
        except (TypeError, ValueError):
            return 0

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
                if tid is not None:
                    grouped.setdefault(tid, []).append(span)
            self._spans_by_trace = grouped
        return self._spans_by_trace

    def _spans_of(self, trace_id: str) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        for span in self._grouped().get(trace_id, []):
            sid = _first(span, "spanId", "span_id")
            if sid is None:
                raise ValueError(f"trace {trace_id!r} has a span with no spanId")
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
            pid = _first(span, "parentSpanId", "parent_span_id") or None
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
                sid, attrs, node_index, prerank, parent_span
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
                    if _first(link, "traceId", "trace_id") != own_trace:
                        continue
                    lsid = _first(link, "spanId", "span_id")
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
    ) -> tuple[str | None, EdgeOrigin]:
        """The single chosen parent cause: the logical graph parent if resolvable, else the
        declared span parent.

        Resolving ``graph.node.parent_id`` to a span never gates on the clock (which would
        silently drop a real parent under skew or a timestamp tie):

        * a single span carries that ``graph.node.id`` → trust it outright (unambiguous);
        * several do (a loop) → pick the nearest *preceding* execution.

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
                preceding = [c for c in candidates if prerank[c] < prerank[sid]]
                if preceding:
                    return max(preceding, key=lambda c: prerank[c]), EdgeOrigin.GRAPH_PARENT
                if len(candidates) == 1:
                    return candidates[0], EdgeOrigin.GRAPH_PARENT
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
