"""Ingestion adapters: external trace source -> RawTrace."""

from tracegraph.adapters.base import TraceAdapter
from tracegraph.adapters.langgraph_checkpoint import LangGraphCheckpointAdapter

__all__ = ["TraceAdapter", "LangGraphCheckpointAdapter"]
