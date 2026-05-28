"""Ingest OpenInference / OTLP spans into a :class:`RawTrace`.

Parses an exported OTLP trace document (the protobuf-JSON shape with
``resourceSpans[].scopeSpans[].spans[]`` emitted by Arize Phoenix, Langfuse, the
OpenTelemetry Collector, etc.) and reconstructs one trace's *raw* causal graph. One span
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
from pathlib import Path
from typing import Any, Iterator

from tracegraph.model import (
    Edge,
    EdgeType,
    RawTrace,
    Step,
    StepKind,
    StepSource,
    StepStatus,
    Trace,
)

_SPAN_KIND_ATTR = "openinference.span.kind"
_GRAPH_NODE_ID = "graph.node.id"
_GRAPH_NODE_PARENT_ID = "graph.node.parent_id"

#: OpenInference span kinds that map cleanly onto a :class:`StepKind`. Anything else
#: (EMBEDDING, RERANKER, GUARDRAIL, EVALUATOR, or absent) defaults to ``CHAIN`` — see
#: the ``StepKind`` docstring: spans that can't be classified more precisely are chains.
_KIND_MAP = {
    "AGENT": StepKind.AGENT,
    "CHAIN": StepKind.CHAIN,
    "TOOL": StepKind.TOOL,
    "LLM": StepKind.LLM,
    "RETRIEVER": StepKind.RETRIEVER,
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
    out: dict[str, Any] = {}
    for a in span.get("attributes") or []:
        key = a.get("key")
        if key is not None:
            out[key] = _attr_value(a.get("value"))
    return out


def _iter_spans(document: dict) -> Iterator[dict]:
    for rs in _first(document, "resourceSpans", "resource_spans", default=[]) or []:
        for ss in _first(rs, "scopeSpans", "scope_spans", default=[]) or []:
            yield from ss.get("spans") or []


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
    if st.get("code") not in _ERROR_CODES:
        return StepStatus.OK, None
    return StepStatus.ERROR, st.get("message") or _exception_message(span) or "error"


def _iso_ts(start_nano: Any) -> str | None:
    try:
        nanos = int(start_nano)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc).isoformat()


class OTLPSpanAdapter:
    """Reconstruct one trace's raw causal graph from an OTLP/OpenInference span export."""

    def __init__(
        self,
        document: dict,
        *,
        source_kind: str = "otlp",
        prefer_graph_parent: bool = True,
        links_as_causes: bool = True,
    ) -> None:
        self._doc = document
        self._source_kind = source_kind
        #: Prefer ``graph.node.parent_id`` (logical agent graph) over ``parentSpanId``.
        self._prefer_graph_parent = prefer_graph_parent
        #: Treat OTLP span links as additional causes (the multi-parent fan-in source).
        self._links_as_causes = links_as_causes

    @classmethod
    def from_json(cls, text: str, **kwargs: Any) -> "OTLPSpanAdapter":
        return cls(json.loads(text), **kwargs)

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "OTLPSpanAdapter":
        return cls.from_json(Path(path).read_text(encoding="utf-8"), **kwargs)

    # --- TraceAdapter ---

    def discover(self) -> list[str]:
        """Distinct ``traceId``s present in the document, in first-seen order."""
        seen: list[str] = []
        for span in _iter_spans(self._doc):
            tid = _first(span, "traceId", "trace_id")
            if tid is not None and tid not in seen:
                seen.append(tid)
        return seen

    def ingest(self, trace_id: str) -> RawTrace:
        by_id = self._spans_of(trace_id)
        parent_span = self._structural_parents(trace_id, by_id)
        depth = self._depths(parent_span)
        attrs = {sid: _attributes(by_id[sid]) for sid in by_id}

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
        causes = self._causes(by_id, attrs, prerank, parent_span)
        seq = self._topo_seq(by_id, causes, prerank)

        steps: list[Step] = []
        for sid in by_id:
            span = by_id[sid]
            status, err = _status(span)
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
                    error_msg=err,
                )
            )
        steps.sort(key=lambda s: s.seq)

        edges = [
            Edge(type=EdgeType.CAUSED_BY, src=effect, dst=cause)
            for effect in sorted(causes, key=lambda s: prerank[s])
            for cause in sorted(causes[effect], key=lambda c: prerank[c])
        ]
        any_error = any(s.status is StepStatus.ERROR for s in steps)
        trace = Trace(
            trace_id=trace_id,
            source_kind=self._source_kind,
            thread_id=trace_id,
            status=StepStatus.ERROR if any_error else StepStatus.OK,
        )
        return RawTrace(trace=trace, steps=steps, causal_edges=edges)

    # --- helpers ---

    @staticmethod
    def _start_nano(span: dict) -> int:
        try:
            return int(_first(span, "startTimeUnixNano", "start_time_unix_nano"))
        except (TypeError, ValueError):
            return 0

    def _spans_of(self, trace_id: str) -> dict[str, dict]:
        by_id: dict[str, dict] = {}
        for span in _iter_spans(self._doc):
            if _first(span, "traceId", "trace_id") != trace_id:
                continue
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
        depth: dict[str, int] = {}
        for sid in parent_span:
            seen: set[str] = set()
            d, cur = 0, parent_span[sid]
            while cur is not None:
                if cur in seen:
                    raise ValueError(f"cycle in parentSpanId chain at span {cur!r}")
                seen.add(cur)
                d += 1
                cur = parent_span[cur]
            depth[sid] = d
        return depth

    def _causes(
        self,
        by_id: dict[str, dict],
        attrs: dict[str, dict],
        prerank: dict[str, int],
        parent_span: dict[str, str | None],
    ) -> dict[str, list[str]]:
        """Reconstruct ``effect -> [causes]`` from declared signals: one parent + links."""
        node_index: dict[str, list[str]] = {}
        if self._prefer_graph_parent:
            for sid in sorted(by_id, key=lambda s: prerank[s]):  # prerank order
                nid = attrs[sid].get(_GRAPH_NODE_ID)
                if nid is not None:
                    node_index.setdefault(str(nid), []).append(sid)

        causes: dict[str, list[str]] = {sid: [] for sid in by_id}
        seen: set[tuple[str, str]] = set()

        def add(effect: str, cause: str) -> None:
            if cause == effect or (effect, cause) in seen:
                return  # self-edges and duplicates would both break validate_raw
            seen.add((effect, cause))
            causes[effect].append(cause)

        for sid in by_id:
            parent = self._resolve_parent(sid, attrs, node_index, prerank, parent_span)
            if parent is not None:
                add(sid, parent)
            if self._links_as_causes:
                own_trace = _first(by_id[sid], "traceId", "trace_id")
                for link in _first(by_id[sid], "links", default=[]) or []:
                    # A link is a declared cause only if it explicitly names THIS trace and an
                    # in-trace span. A missing/foreign traceId is NOT silently treated as local
                    # (that would fabricate causality). Declared links are trusted regardless of
                    # timestamp — topo ordering sequences them and a real cycle raises.
                    if _first(link, "traceId", "trace_id") != own_trace:
                        continue
                    lsid = _first(link, "spanId", "span_id")
                    if lsid in by_id:
                        add(sid, lsid)
        return causes

    def _resolve_parent(
        self,
        sid: str,
        attrs: dict[str, dict],
        node_index: dict[str, list[str]],
        prerank: dict[str, int],
        parent_span: dict[str, str | None],
    ) -> str | None:
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
                    return max(preceding, key=lambda c: prerank[c])
                if len(candidates) == 1:
                    return candidates[0]
                if pid is None:
                    detail = "no in-trace span" if not candidates else "multiple ambiguous spans"
                    raise ValueError(
                        f"span {sid!r} declares graph.node.parent_id={pnode!r} that resolves to "
                        f"{detail} and has no parentSpanId fallback; tracegraph will not "
                        "fabricate a root for a declared logical parent it cannot honor"
                    )
        return pid

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
