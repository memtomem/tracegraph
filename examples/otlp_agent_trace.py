"""A small but realistic OpenInference/OTLP span trace used to exercise the OTLP adapter.

Unlike a LangGraph super-step chain (strictly linear), span traces are a true DAG: this
sample has a **fan-in** so it stresses tracegraph's raw/derived split. The logical agent
graph (via ``graph.node.parent_id``) is::

    agent ─▶ plan ─┬─▶ retrieve_docs
                   ├─▶ web_search   (✗ tool error)
                   └─▶ synthesize   ◀── link ── retrieve_docs

``synthesize`` is therefore caused by **two** real predecessors — its graph parent
``plan`` *and* the linked ``retrieve_docs`` span — so it is flagged ``projection_lossy``:
the single-parent tree view must drop one of those causes, while ``explain`` (raw layer)
still surfaces both.

Nothing here imports tracegraph; it just emits the OTLP/JSON shape an exporter would.
"""

from __future__ import annotations

import json
from pathlib import Path

_TRACE_ID = "agent-trace-1"
_BASE_NANO = 1_700_000_000_000_000_000


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _span(
    span_id: str,
    name: str,
    kind: str,
    *,
    parent_span: str | None,
    node_id: str,
    node_parent: str | None,
    offset_ns: int,
    status: dict | None = None,
    links: list[dict] | None = None,
) -> dict:
    attrs = [_attr("openinference.span.kind", kind), _attr("graph.node.id", node_id)]
    if node_parent is not None:
        attrs.append(_attr("graph.node.parent_id", node_parent))
    span: dict = {
        "traceId": _TRACE_ID,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(_BASE_NANO + offset_ns),
        "endTimeUnixNano": str(_BASE_NANO + offset_ns + 1_000),
        "attributes": attrs,
    }
    if parent_span is not None:
        span["parentSpanId"] = parent_span
    if status is not None:
        span["status"] = status
    if links is not None:
        span["links"] = links
    return span


def sample_otlp_document() -> dict:
    """An OTLP/JSON ``ExportTraceServiceRequest`` containing one fan-in agent trace."""
    spans = [
        _span("s0", "agent", "AGENT", parent_span=None, node_id="agent", node_parent=None, offset_ns=0),
        _span("s1", "plan", "CHAIN", parent_span="s0", node_id="plan", node_parent="agent", offset_ns=10),
        _span("s2", "retrieve_docs", "RETRIEVER", parent_span="s0", node_id="retrieve", node_parent="plan", offset_ns=20),
        _span(
            "s3", "web_search", "TOOL",
            parent_span="s0", node_id="web_search", node_parent="plan", offset_ns=30,
            status={"code": "STATUS_CODE_ERROR", "message": "tool failed: upstream timeout"},
        ),
        _span(
            "s4", "synthesize", "LLM",
            parent_span="s0", node_id="synthesize", node_parent="plan", offset_ns=40,
            links=[{"traceId": _TRACE_ID, "spanId": "s2"}],  # synthesize also consumed retrieval
        ),
    ]
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


if __name__ == "__main__":  # pragma: no cover - manual demo
    out = Path("otlp_trace.json")
    out.write_text(json.dumps(sample_otlp_document(), indent=2), encoding="utf-8")
    print(f"wrote {out} (traceId={_TRACE_ID}) — try: tracegraph ingest-otlp -f {out}")
