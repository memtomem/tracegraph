"""Body-free ingestion of trace JSON exported by the official Phoenix CLI."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from tracegraph.adapters.base import Disclosures
from tracegraph.adapters.otlp_spans import (
    OTLPSpanAdapter,
    _attributes,
    _first,
    _parse_nanos,
    _span_array,
)
from tracegraph.input_validation import loads, object_field
from tracegraph.model import RawTrace

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


#: The smallest number read as epoch nanoseconds: 1973-03-03T09:46:40Z. Seconds, milliseconds and
#: microseconds for any date before the year 5138 all fall below it, so a smaller number is in
#: some other unit, and which one cannot be told from its size.
_MIN_EPOCH_NANOS = 10**17
#: Handed on for a number below the floor. ``int()`` cannot read it, so the span builder reports
#: the time as unavailable and counts it under ``otlp.unreadable_timestamp``. It is fixed text, so
#: no producer code (a ``__repr__``) runs, and only the parsed time ever reaches the artifact.
_NOT_EPOCH_NANOS = "not epoch nanoseconds"


def _epoch_nanos(number: int) -> str:
    return str(number) if number >= _MIN_EPOCH_NANOS else _NOT_EPOCH_NANOS


def _nanos(value: Any) -> Any:
    """Phoenix's time to OTLP nanoseconds. A value that cannot be converted, or a number too
    small to be epoch nanoseconds, is handed on in a form the span builder cannot read, so it
    reports the time as unavailable *and* discloses it."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return value
    if isinstance(value, (int, float)):
        return _epoch_nanos(int(value))
    text = str(value)
    if text.isdigit():
        try:
            return _epoch_nanos(int(text))
        except ValueError:  # a non-ASCII digit, or past the interpreter's int-string limit
            return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Whatever the span builder would still read as a time (a sign, whitespace, underscores,
        # bytes) is a number, so the floor applies to it too. Asking the builder's own parser
        # keeps the two from disagreeing about what is a number.
        number = _parse_nanos(value)
        return value if number is None else _epoch_nanos(number)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp() * 1_000_000_000))


def _start_order(value: Any) -> int | float | None:
    """A start time below the epoch-nanosecond floor, as the number it is, or ``None``.

    Its unit is unknown, so it is no time, but within one export it still orders spans: the
    span builder uses it only to choose among several candidate loop parents, and discloses
    that it did. It follows ``_nanos``'s branches, so an ISO string (a compact one such as
    ``20260714_123456`` included) never reaches the number path. A bool or bytes value is not
    taken as a number here, although ``_nanos`` refuses those as times too.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) and int(value) < _MIN_EPOCH_NANOS else None
    if isinstance(value, int):
        return value if value < _MIN_EPOCH_NANOS else None
    if not isinstance(value, str):
        return None
    number: int | None
    if value.isdigit():
        try:
            number = int(value)
        except ValueError:  # a non-ASCII digit, or past the interpreter's int-string limit
            return None
    else:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return None
        except ValueError:
            number = _parse_nanos(value)
    return number if number is not None and number < _MIN_EPOCH_NANOS else None


_OPERATION_NAME = re.compile(r"[A-Za-z0-9_.:/#@-]{1,200}")


def _safe_operation_name(value: Any) -> str | None:
    if value is None:
        return None
    # Operation identifiers are useful, but natural-language span names can themselves be
    # prompts. Keep identifier-shaped names only; reports fall back to the normalized kind.
    clean = str(value).strip()
    return clean if _OPERATION_NAME.fullmatch(clean) else None




def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    raw = _attributes(span)
    safe = {key: raw[key] for key in _SAFE_ATTRIBUTES if key in raw}
    kind = _first(span, "span_kind", "spanKind") or raw.get("openinference.span.kind")
    if kind is not None:
        safe["openinference.span.kind"] = kind
    return safe


def _as_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if math.isfinite(score) else None


def _evaluations(span: dict[str, Any]) -> list[dict[str, Any]]:
    """The span's evaluations, each with ``unreadable_score`` set when a score was sent but
    cannot be represented; the caller strips that key and discloses the count."""
    out: list[dict[str, Any]] = []
    annotations = span.get("annotations")
    if annotations is None:
        annotations = []
    if not isinstance(annotations, list):
        raise ValueError("Phoenix spans[].annotations must be an array")
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        result = annotation.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        # Annotation names and labels obey the same rule as span names: identifier-shaped text
        # only, because a free-text label is commonly an evaluator's note about the content.
        # Without a usable name an evaluation has no identity, so it is dropped entirely.
        name = _safe_operation_name(annotation.get("name"))
        if name is None:
            continue
        label = _first(result, "label") or annotation.get("label")
        sent = _first(result, "score")
        if sent is None:
            sent = annotation.get("score")
        score = _as_score(sent)
        out.append({"name": name, "label": _safe_operation_name(label), "score": score,
                    "unreadable_score": sent is not None and score is None})
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
        """Reads the whole file: the CLI's ``TRACEGRAPH_MAX_ARTIFACT_BYTES`` cap does not apply here."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def discover(self) -> list[str]:
        return list(self._ids)

    def ingest(self, trace_id: str) -> RawTrace:
        try:
            trace = self._by_id[trace_id]
        except KeyError as exc:
            raise KeyError(f"no Phoenix trace {trace_id!r}") from exc
        spans = [self._span(trace_id, span) for span in trace["spans"]]
        unreadable_scores = sum(
            item.pop("unreadable_score") for span in spans for item in span["_tracegraph_evaluations"]
        )
        document = {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}
        raw = OTLPSpanAdapter(
            document,
            source_kind="phoenix_cli",
            links_as_causes=False,
            include_error_messages=False,
            preserve_unset_status=True,
            evaluation_channel="_tracegraph_evaluations",
            start_order_channel="_tracegraph_start_order",
        ).ingest(trace_id)
        if unreadable_scores:
            disclosures = Disclosures()
            disclosures.add(
                "phoenix.unreadable_evaluation_score",
                f"{unreadable_scores} evaluation score(s) cannot be read as a finite number; "
                "each is reported as null.",
                unreadable_scores,
            )
            disclosures.apply(raw)
        return raw

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
            "_tracegraph_start_order": _start_order(_first(span, "start_time", "startTime")),
        }
