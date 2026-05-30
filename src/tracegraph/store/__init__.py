"""Pluggable query backends. The pure-Python ``InMemoryStore`` is the default.

The optional :class:`KuzuStore` lives behind the ``[cypher]`` extra and is exported here
**only** when ``kuzu`` is importable, so a default install never pays the import cost (or
errors on a missing optional dep).
"""

from tracegraph.store.base import GraphStore
from tracegraph.store.in_memory import InMemoryStore

__all__ = ["GraphStore", "InMemoryStore"]

try:
    from tracegraph.store.kuzu import KuzuStore  # noqa: F401
except ModuleNotFoundError as exc:
    # Swallow ONLY "the optional dep itself is missing"; anything else (a broken kuzu
    # install, a typo in our own imports, a removed internal module) must surface as a
    # real error, not silently hide KuzuStore from the public API.
    if exc.name != "kuzu":
        raise
else:
    __all__.append("KuzuStore")
