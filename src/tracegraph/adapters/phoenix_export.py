"""Body-free ingestion of trace JSON exported by the official Phoenix CLI."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from tracegraph.adapters.otlp_spans import OTLPSpanAdapter, _span_array
from tracegraph.input_validation import loads, object_field
from tracegraph.model import CausalFidelity, RawTrace

_SAFE_ATTRIBUTES = {
    "graph.node.id",
    "graph.node.parent_id",
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.token_count.total",
    "llm.token_count.input",
    "llm.token_count.output",
    "llm.cost.prompt",
    "llm.cost.completion",
    "llm.cost.total",
    "llm.cost.input",
    "llm.cost.output",
    "llm.cost.currency",
}


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _nanos(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value)
    if text.isdigit():
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return str(int(parsed.timestamp() * 1_000_000_000))


_OPERATION_NAME = re.compile(r"[A-Za-z0-9_.:/#@-]{1,200}")


def _safe_operation_name(value: Any) -> str | None:
    if value is None:
        return None
    # Operation identifiers are useful, but natural-language span names can themselves be
    # prompts. Keep identifier-shaped names only; reports fall back to the normalized kind.
    clean = str(value).strip()
    return clean if _OPERATION_NAME.fullmatch(clean) else None


def _safe_evaluation_text(value: Any) -> str | None:
    if value is None:
        return None
    clean = " ".join(str(value).split())
    return clean[:200] or None


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    from tracegraph.adapters.otlp_spans import _attributes
    raw = _attributes(span)
    safe = {key: raw[key] for key in _SAFE_ATTRIBUTES if key in raw}
    kind = _first(span, "span_kind", "spanKind") or raw.get("openinference.span.kind")
    if kind is not None:
        safe["openinference.span.kind"] = kind
    return safe


def _evaluations(span: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for annotation in span.get("annotations") or []:
        if not isinstance(annotation, dict):
            continue
        result = annotation.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        name = annotation.get("name")
        if not name:
            continue
        label = _first(result, "label") or annotation.get("label")
        score = _first(result, "score")
        if score is None:
            score = annotation.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        if score is not None and not math.isfinite(score):
            score = None
        out.append(
            {
                "name": _safe_evaluation_text(name),
                "label": _safe_evaluation_text(label),
                "score": score,
            }
        )
    return out


def _trace_objects(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        candidates = document
    elif isinstance(document, dict) and isinstance(document.get("traces"), list):
        candidates = document["traces"]
    elif isinstance(document, dict) and isinstance(document.get("data"), list):
        candidates = document["data"]
    elif isinstance(document, dict) and isinstance(document.get("spans"), list):
        candidates = [document]
    else:
        raise ValueError("Phoenix export must be a trace object or an array of trace objects")
    if not all(isinstance(item, dict) and isinstance(item.get("spans"), list) for item in candidates):
        raise ValueError("every Phoenix trace must be an object with a spans array")
    return candidates


class PhoenixExportAdapter:
    """Convert Phoenix CLI trace DTOs through the shared OTLP causal builder."""

    def __init__(self, document: Any) -> None:
        self._traces = _trace_objects(document)
        self._ids: list[str] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        for trace in self._traces:
            trace_id = self._trace_id(trace)
            if trace_id in self._by_id:
                raise ValueError(f"duplicate Phoenix traceId {trace_id!r}")
            self._ids.append(trace_id)
            self._by_id[trace_id] = trace

    @classmethod
    def from_json(cls, text: str) -> "PhoenixExportAdapter":
        return cls(loads(text))

    @classmethod
    def from_file(cls, path: str | Path) -> "PhoenixExportAdapter":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def discover(self) -> list[str]:
        return list(self._ids)

    def ingest(self, trace_id: str) -> RawTrace:
        try:
            trace = self._by_id[trace_id]
        except KeyError as exc:
            raise KeyError(f"no Phoenix trace {trace_id!r}") from exc
        spans = [self._span(trace_id, span) for span in trace["spans"]]
        document = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
        return OTLPSpanAdapter(
            document,
            source_kind="phoenix_cli",
            links_as_causes=False,
            include_error_messages=False,
            preserve_unset_status=True,
            causal_fidelity=CausalFidelity.PARENT_ONLY,
            links_preserved=False,
        ).ingest(trace_id)

    @staticmethod
    def _trace_id(trace: dict[str, Any]) -> str:
        declared = _first(trace, "traceId", "trace_id")
        span_ids = set()
        for span in trace["spans"]:
            if not isinstance(span, dict):
                raise ValueError("spans[] must be an object")
            context = object_field(span.get("context"), "spans[].context")
            sid = _first(context, "trace_id", "traceId")
            if sid is not None and not isinstance(sid, str):
                raise ValueError("spans[].context.trace_id must be a string")
            span_ids.add(sid)
        span_ids.discard(None)
        if declared is None and len(span_ids) == 1:
            declared = next(iter(span_ids))
        if not isinstance(declared, str) or not declared:
            raise ValueError("Phoenix trace has no usable traceId")
        if span_ids and span_ids != {declared}:
            raise ValueError(f"Phoenix trace {declared!r} contains spans from another trace")
        return declared

    @staticmethod
    def _span(trace_id: str, span: Any) -> dict[str, Any]:
        if not isinstance(span, dict):
            raise ValueError(f"Phoenix trace {trace_id!r} contains a non-object span")
        context = object_field(span.get("context"), "spans[].context")
        span_id = _first(context, "span_id", "spanId") or _first(span, "id", "span_id", "spanId")
        if not isinstance(span_id, str) or not span_id:
            raise ValueError(f"Phoenix trace {trace_id!r} contains a span with no id")
        status = _first(span, "status_code", "statusCode")
        return {
            "traceId": trace_id,
            "spanId": span_id,
            "parentSpanId": _first(span, "parent_id", "parentId"),
            "name": _safe_operation_name(span.get("name")),
            "startTimeUnixNano": _nanos(_first(span, "start_time", "startTime")),
            "endTimeUnixNano": _nanos(_first(span, "end_time", "endTime")),
            "status": {
                "code": status,
                # Read transiently for status normalization; Phoenix imports never persist it.
                "message": _first(span, "status_message", "statusMessage"),
            },
            "attributes": _attrs(span),
            "events": _span_array(span, "events"),
            "_tracegraph_evaluations": _evaluations(span),
        }
