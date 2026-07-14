"""Vendor-neutral, fail-open producer contract for optional SyncMill telemetry.

This tiny reference is deliberately independent of tracegraph and OpenTelemetry packages so
SyncMill can vendor the behavior without creating a runtime dependency. Instrumentation is
disabled by default; when enabled, exporter failures are observable through the boolean return
value but never replace the operation's result or exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


Exporter = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class BestEffortTelemetry:
    enabled: bool = False
    exporter: Exporter | None = None

    def emit(self, document: dict[str, Any]) -> bool:
        if not self.enabled or self.exporter is None:
            return False
        try:
            self.exporter(document)
        except Exception:
            return False
        return True


def run_with_telemetry(
    operation: Callable[[], Any],
    telemetry: BestEffortTelemetry,
    document: dict[str, Any],
) -> Any:
    """Run business logic first and export best-effort without changing its result."""
    result = operation()
    telemetry.emit(document)
    return result
