"""Pluggable query backends. The pure-Python ``InMemoryStore`` is the default.

The optional :class:`LadybugStore` lives behind the ``[cypher]`` extra and is exported here
**only** when ``ladybug`` is importable, so a default install never pays the import cost (or
errors on a missing optional dep).
"""

from tracegraph.store.base import GraphStore
from tracegraph.store.in_memory import InMemoryStore

__all__ = ["GraphStore", "InMemoryStore"]

try:
    from tracegraph.store.ladybug import LadybugStore  # noqa: F401
except ModuleNotFoundError as exc:
    # Swallow ONLY "the optional dep itself is missing"; anything else (a broken ladybug
    # install, a typo in our own imports, a removed internal module) must surface as a
    # real error, not silently hide LadybugStore from the public API.
    if exc.name != "ladybug":
        raise
else:
    __all__.append("LadybugStore")
