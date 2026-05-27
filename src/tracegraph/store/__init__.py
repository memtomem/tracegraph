"""Pluggable query backends. The pure-Python ``InMemoryStore`` is the default."""

from tracegraph.store.base import GraphStore
from tracegraph.store.in_memory import InMemoryStore

__all__ = ["GraphStore", "InMemoryStore"]
