"""Ingestion adapters: external trace source -> RawTrace."""

from tracegraph.adapters.base import TraceAdapter
from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter
from tracegraph.adapters.otlp_spans import OTLPSpanAdapter
from tracegraph.adapters.phoenix_export import PhoenixExportAdapter

__all__ = [
    "TraceAdapter",
    "LangGraphCheckpointAdapter",
    "OTLPSpanAdapter",
    "PhoenixExportAdapter",
]
