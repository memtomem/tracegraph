"""The adapter interface: turn an external trace source into a :class:`RawTrace`.

An adapter never computes the derived tree — it only reconstructs the raw causal graph
(steps + ``CAUSED_BY`` edges). ``normalize`` does the rest.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tracegraph.model import Capture, RawTrace


class Disclosures:
    """Ingestion warnings and their capture-gap codes, recorded together.

    Every disclosure goes through :meth:`add`, so a warning cannot be printed without the code
    that keeps it in the artifact (``tests/test_capture_provenance.py`` enforces this).
    """

    def __init__(self) -> None:
        self._texts: list[str] = []
        self._counts: dict[str, int] = {}

    @property
    def texts(self) -> tuple[str, ...]:
        """The warning text, read-only so nothing can be added without its code."""
        return tuple(self._texts)

    def add(self, code: str, text: str, count: int = 1) -> None:
        self._texts.append(text)
        self._counts[code] = self._counts.get(code, 0) + count

    def extend(self, other: "Disclosures") -> None:
        self._texts.extend(other._texts)
        for code, count in other._counts.items():
            self._counts[code] = self._counts.get(code, 0) + count

    def capture(self) -> Capture:
        return Capture.from_counts(self._counts)

    def apply(self, raw: RawTrace) -> None:
        """Add these disclosures to a trace another reader already produced."""
        counts = dict(self._counts)
        for gap in raw.trace.capture.gaps if raw.trace.capture else ():
            counts[gap.code] = counts.get(gap.code, 0) + gap.count
        raw.trace.capture = Capture.from_counts(counts)
        raw.ingest_warnings.extend(self.texts)


@runtime_checkable
class TraceAdapter(Protocol):
    def discover(self) -> list[str]:
        """Best-effort list of available trace ids (may be unsupported by a source)."""
        ...

    def ingest(self, trace_id: str) -> RawTrace:
        """Reconstruct one trace's raw causal graph. Raises ``KeyError`` if not found."""
        ...
